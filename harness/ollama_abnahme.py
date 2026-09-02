#!/usr/bin/env python3
"""Abnahme nach dem Ollama-Upgrade: laeuft Tims Anlage noch?

Ein Sprung von 0.32 auf 0.33 kann das Verhalten der Modelle aendern -
und lagunas Modelfile haengt an RENDERER/PARSER statt an einem eigenen
TEMPLATE. Wenn das anders behandelt wird, merkt man es nicht am
Absturz, sondern an schlechteren Antworten. Das ist die schlimmste
Sorte Aenderung, also wird sie gemessen.
"""
import json, pathlib, sys, time, urllib.request

OLLAMA = "http://127.0.0.1:11434"
ZENTRALE = "http://127.0.0.1:8770"
fehler = []


def pruefe(b, was, zusatz=""):
    print("  %-7s %s%s" % ("ok" if b else "FEHLER", was,
                           "" if b else "  <- " + str(zusatz)[:160]))
    if not b:
        fehler.append(was)


def ollama(pfad, koerper=None, geduld=300):
    daten = json.dumps(koerper).encode() if koerper else None
    a = urllib.request.Request(OLLAMA + pfad, data=daten,
                               method="POST" if daten else "GET",
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(a, timeout=geduld) as r:
        return json.loads(r.read().decode())


print("Abnahme nach dem Ollama-Upgrade:")

# 1. Dienst und Bestand
try:
    tags = ollama("/api/tags")
    namen = sorted(m["name"] for m in tags.get("models", []))
    pruefe(len(namen) >= 4, "alle Modelle sind noch da: %s" % namen, namen)
except Exception as f:
    pruefe(False, "Ollama antwortet", "%s: %s" % (type(f).__name__, f))
    sys.exit(1)

# 2. laguna antwortet sinnvoll - der RENDERER/PARSER-Test
t0 = time.time()
d = ollama("/api/chat", {"model": "laguna-xs-2.1", "stream": False,
                         "messages": [{"role": "user",
                                       "content": "Wie viel ist 7 mal 8? "
                                                  "Antworte nur mit der Zahl."}],
                         "options": {"num_ctx": 8192, "num_predict": 512}})
inhalt = ((d.get("message") or {}).get("content") or "").strip()
pruefe("56" in inhalt, "laguna rechnet richtig (RENDERER/PARSER intakt)",
       repr(inhalt[:80]))
pruefe(len(inhalt) < 200, "und antwortet knapp statt zu schwafeln",
       "%d Zeichen" % len(inhalt))
print("        (%.1f s)" % (time.time() - t0))

# 3. Werkzeugaufruf - daran haengt Tims ganzer Chat
WERKZEUG = [{"type": "function", "function": {
    "name": "wetter", "description": "Nennt das Wetter fuer einen Ort.",
    "parameters": {"type": "object",
                   "properties": {"ort": {"type": "string"}},
                   "required": ["ort"]}}}]
d = ollama("/api/chat", {"model": "laguna-xs-2.1", "stream": False,
                         "tools": WERKZEUG,
                         "messages": [{"role": "user",
                                       "content": "Wie ist das Wetter in "
                                                  "Muenchen? Benutze das "
                                                  "Werkzeug."}],
                         "options": {"num_ctx": 8192, "num_predict": 512}})
rufe = (d.get("message") or {}).get("tool_calls") or []
pruefe(len(rufe) == 1, "laguna ruft das Werkzeug auf", str(rufe)[:120])
if rufe:
    arg = rufe[0].get("function", {}).get("arguments") or {}
    pruefe("uenchen" in json.dumps(arg, ensure_ascii=False),
           "und uebergibt das Argument richtig", str(arg))

# 4. Der Neuzugang laedt auch noch.
#
# num_predict 8192, NICHT 64: Beim ersten Lauf am 02.09.2026 stand hier
# 64, und gemma4 antwortete leer - done_reason "length". Nicht das
# Modell war schuld und nicht das Upgrade, sondern dieser Testfall: Der
# Denkweg verbraucht das Budget, fuer die Antwort bleibt nichts. Genau
# die Falle, vor der MODELL_GRENZEN in m1_zentrale warnt ("fehlendes
# num_predict = leere Antwort nach langem Denken") - und ich bin in
# meiner eigenen Abnahme hineingelaufen.
d = ollama("/api/chat", {"model": "gemma4:26b-a4b-it-qat", "stream": False,
                         "messages": [{"role": "user",
                                       "content": "Sag nur: bereit."}],
                         "options": {"num_ctx": 8192, "num_predict": 8192}})
inh_g = ((d.get("message") or {}).get("content") or "").strip()
pruefe(bool(inh_g), "gemma4 antwortet ebenfalls",
       "done_reason=%s" % d.get("done_reason"))
pruefe(d.get("done_reason") != "length",
       "und zwar ohne am Antwortbudget zu ersticken")

# 5. Tims eigener Weg - ueber die Zentrale, nicht Ollama direkt
try:
    # Kein fester Pfad mit Benutzernamen - das Repo ist oeffentlich,
    # und die Datenschutz-Pruefung hat diesen Commit zu Recht
    # abgelehnt.
    token = (pathlib.Path.home() / ".m1_job_token").read_text().strip()
    a = urllib.request.Request(
        ZENTRALE + "/api/chat",
        data=json.dumps({"modell": "laguna-xs-2.1",
                         "nachrichten": [{"role": "user",
                                          "content": "Antworte in einem "
                                                     "kurzen Satz: Wie "
                                                     "heisst du?"}]}).encode(),
        method="POST", headers={"Content-Type": "application/json",
                                "X-M1-Token": token})
    with urllib.request.urlopen(a, timeout=600) as r:
        z = json.loads(r.read().decode())
    antwort = str(z.get("antwort") or "")
    pruefe(len(antwort.strip()) > 5, "Tims Chat ueber die Zentrale antwortet",
           repr(antwort[:100]))
    print("        Antwort: %s" % " ".join(antwort.split())[:120])
except Exception as f:
    pruefe(False, "Tims Chat ueber die Zentrale",
           "%s: %s" % (type(f).__name__, f))

print("\n%s" % ("Abnahme bestanden." if not fehler
                else "%d FEHLER - Rueckweg: brew unlink ollama && "
                     "brew link ollama@0.32.14 (alte Fassung liegt im "
                     "Cellar)" % len(fehler)))
sys.exit(1 if fehler else 0)
