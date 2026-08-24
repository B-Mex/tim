# Tim — lokaler KI-Server für Apple Silicon

Tim ist ein komplett lokaler KI-Assistent für den Mac (Apple Silicon). Keine Cloud,
keine API-Schlüssel, keine Abogebühren: Alle Modelle laufen über [Ollama](https://ollama.com)
auf dem eigenen Rechner, alle Daten bleiben im Haus.

**Was Tim kann:**

- **Zentrale** (`oberflaeche/`) — Browser-Oberfläche auf Port 8770: Chat mit lokalen
  Modellen, Modell-Umschalter, Dienste-Status, Knöpfe für Abläufe.
- **Sprachassistent „Hey Tim“** (`scripts/`) — Wake-Word übers Mikrofon,
  Whisper (lokal) für Spracherkennung, Piper für die deutsche Sprachausgabe.
- **Tims Auge** (`kamera/`) — Webcam-Dienst auf Port 8781 mit YOLOE-Objekterkennung
  (open-vocabulary: frei wählbare Begriffe statt fester COCO-Klassen).
- **Agenten-Harness** (`harness/`) — CrewAI-Abläufe als JSON-Jobs mit Schema-Prüfung,
  Autonomie-Regeln, Canary-Check und Selbsttests. Recherchiert und schlägt vor —
  kauft nie, löscht nie ohne Backup.
- **Modell-Benchmark** (`harness/modell_benchmark.py`) — misst jedes neue Modell mit
  pflegbaren Testfällen; jeder Testfall braucht den Zwei-Seiten-Beweis
  (gute Antwort besteht, schlechte fällt durch).
- **Gedächtnis** (`memory/`, wird lokal angelegt) — Chatablage als JSONL plus
  ChromaDB für semantische Suche.
- **Websuche** (optional) — über eine lokale SearXNG-Instanz; der Chat selbst
  bleibt offline, nur ausgewiesene Abläufe dürfen suchen.

**Referenz-Hardware:** Mac Studio M1 Max, 32 GB RAM, macOS. Mit kleineren Modellen
läuft Tim auch auf 16 GB — die großen Modelle unten brauchen die vollen 32 GB.

---

## Verzeichnisstruktur

| Ordner | Inhalt |
|---|---|
| `oberflaeche/` | Zentrale (Port 8770) und Job-Server (Port 8765) |
| `scripts/` | Sprachassistent „Hey Tim“ |
| `kamera/` | Kamera-Dienst „Tims Auge“ (Port 8781) + Objekterkennung |
| `harness/` | Agenten-Abläufe, Job-Schema, Autonomie, Benchmark, Model-Router |
| `harness/jobs/` | Beispiel-Jobs (Recherche, Projekt-Review, Modell-Scan) |
| `modelfiles/` | Ollama-Modelfiles zum Nachbauen aller Modelle |
| `config/` | Konfiguration; `*.example` kopieren und anpassen |
| `launchagents/` | launchd-Dienste für den Autostart |
| `modelle/`, `whisper-models/`, `piper/` | große Binaries — werden NICHT mitversioniert, Beschaffung siehe unten |
| `memory/`, `logs/`, `venv/` | entstehen zur Laufzeit, bleiben lokal |

Der Pfad `/opt/ki-server` ist in Diensten und Skripten fest verdrahtet —
das Repo genau dorthin klonen.

---

## Installation

### 1. Repo und Grundwerkzeuge

```bash
sudo mkdir -p /opt/ki-server && sudo chown $(whoami) /opt/ki-server
git clone <REPO-URL> /opt/ki-server
cd /opt/ki-server
brew install ollama whisper-cpp python@3.12
brew services start ollama
```

### 2. Python-Umgebungen

```bash
# Haupt-Umgebung (Zentrale, Harness, Kamera):
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt

# Der Sprachassistent läuft bewusst mit dem Homebrew-Python
# (Audio-Pakete machen im venv auf macOS Ärger):
pip3 install --break-system-packages sounddevice numpy scipy
```

### 3. Sprachmodelle (GGUF) beschaffen

Die GGUF-Dateien nach `modelle/` legen. Quellen: [Hugging Face](https://huggingface.co)
(Dateiname exakt so suchen), `muse-agent` kommt direkt aus der Ollama-Registry.

| Ollama-Name | Rolle | Quelle |
|---|---|---|
| `qwen3.6:35b-a3b` | Hauptmodell (Router-Klasse 1+2): Recherche, Code, Controller — Benchmark-Sieger 14/14 | GGUF „Qwen3.6-35B-A3B“, Hugging Face |
| `qwen3.5:9b` | kleines Modell (Router-Klasse 3): Formatchecks, Canary — Standard für Zentrale-Chat und Sprachassistent | GGUF „Qwen3.5-9B“, Hugging Face |
| `qwen3-general` | Denk-Partner mit Thinking (42B) | GGUF-Datei siehe `modelfiles/qwen3-general.modelfile` |
| `qwen3-coder` | Coding-Agent (30B) | GGUF-Datei siehe Modelfile |
| `qwen3.8:27b` | Chat-Modell (Qwen3.5-Basis) | GGUF-Datei siehe Modelfile |
| `muse-agent` | Tool-Calling-Agent | `FROM`-Zeile zieht automatisch aus der Ollama-Registry |
| `llama-fast` | Mini-Modell (3B) — bewusst NICHT im Router: erfand im Test Fakten | GGUF-Datei siehe Modelfile |

Dann je Modell:

```bash
ollama create qwen3.6:35b-a3b -f modelfiles/qwen3.6-35b-a3b.modelfile
```

**Wichtig:** Die Modelfiles der Renderer-Modelle (qwen3.5/3.6/3.8, qwen3-coder,
muse-agent) enthalten bewusst **kein** `TEMPLATE` — `RENDERER`/`PARSER` liefern das
echte Chat-Format. Kein `TEMPLATE {{ .Prompt }}` hineinkopieren, das würde das
Chat-Format zerstören. Ebenso niemals `think: false` an Nur-Denker-Modelle
wie `qwen3-general` senden — sie antworten sonst leer.

### 4. Whisper- und Piper-Dateien

```bash
# Whisper-Modelle (Spracherkennung), von https://huggingface.co/ggerganov/whisper.cpp:
#   ggml-tiny.bin, ggml-base.bin, ggml-medium.bin  ->  whisper-models/

# Piper (Sprachausgabe): macOS-arm64-Build von https://github.com/rhasspy/piper
# nach piper/piper entpacken; deutsche Stimme von
# https://huggingface.co/rhasspy/piper-voices (de_DE-thorsten-high.onnx + .json)
# nach piper/voices/
```

### 5. Konfiguration

```bash
cp config/autonomie.conf.example config/autonomie.conf
# darin die eigene Benachrichtigungs-E-Mail eintragen; Autonomie-Stufen nach Bedarf
```

Optional Home Assistant: `config/ha_token.secret.example` lesen. Tim läuft ohne
Home Assistant vollständig — die beiden Systeme hängen bewusst nicht voneinander ab.

### 6. Dienste einrichten (Autostart)

```bash
cp launchagents/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ki-server.zentrale.plist
launchctl load ~/Library/LaunchAgents/com.ki-server.jobserver.plist
launchctl load ~/Library/LaunchAgents/com.ki-server.sprachassistent.plist
launchctl load ~/Library/LaunchAgents/com.ki-server.kamera.plist
```

Beim ersten Kamera-Start fragt macOS nach der Kamera-Freigabe für Python —
einmal erlauben, danach startet der Dienst ohne Nachfrage.

Wer das große 23-GB-Modell komplett auf der GPU halten will (32-GB-Macs):
`config/com.ki-server.iogpu-limit.plist` — Installationsbefehl steht in der Datei,
den Wert an den eigenen RAM anpassen.

### 7. Loslegen

Zentrale im Browser: `http://127.0.0.1:8770` — das Zugangs-Token wird beim ersten
Start automatisch erzeugt und liegt in `~/.m1_job_token` (nur für den Besitzer lesbar):

```bash
cat ~/.m1_job_token
```

---

## Sicherheit & Autonomie

- **Token-Schutz:** Jeder API-Zugriff (Zentrale, Job-Server, Kamera) verlangt das
  Token aus `~/.m1_job_token`. Die Oberfläche selbst enthält keine Daten.
- **Autonomie standardmäßig „safe“:** Agenten machen nur Vorschläge. Stufen und
  harte Grenzen (nie kaufen, nie ohne Backup löschen) stehen in `config/autonomie.conf`.
- **Kill-Switch:** eine Datei namens `STOP` im Deployment-Ordner stoppt alle
  autonomen Aktionen sofort.
- **Netz-Abwehr:** Agenten-Werkzeuge verweigern Abrufe interner Adressen
  (Loopback, LAN, Tailscale/CGNAT, Link-Local) — gegen präparierte Suchtreffer.
- **Fernzugriff:** nur übers eigene [Tailscale](https://tailscale.com)-Netz
  (z. B. per `tailscale serve` auf Port 8770) — niemals den Port ins offene
  Internet freigeben.

## Erweitern

- **Eigene Abläufe:** JSON nach dem Muster in `harness/jobs/` anlegen;
  `harness/neuer_job.py` hilft, `harness/job_schema.py` prüft (71 Sicherheitstests).
- **Eigene Benchmark-Fälle:** als Daten in `config/benchmark_faelle_extra.json` —
  jede Prüfung braucht eine gute UND eine schlechte Beispielantwort (Gegenprobe).
  Quellenliste für Testideen: `config/benchmark_quellen.json`.
- **Kamera-Begriffe:** `kamera/begriffe.json` — YOLOE erkennt frei benennbare
  Objekte; die Begriffe werden ins Modell „eingebacken“ (siehe
  `kamera/objekterkennung.py`, Basis YOLOE-11L von Ultralytics, AGPL-3.0).
- **Ankerphrase:** Tim beginnt Textchat-Antworten mit dem Anker `Mexla,` — das ist
  die Drift-Erkennung (Canary): fehlt der Anker, stimmt das Modelfile nicht oder
  das Modell driftet. Wer einen anderen Anker will, ändert ihn konsistent in
  `harness/canary_check.py`, `harness/job_schema.py`, `harness/jobs/*.json`,
  `oberflaeche/m1_zentrale.py`, `oberflaeche/zentrale.html` und
  `scripts/sprachassistent.py` — und lässt danach die Selbsttests laufen:

  ```bash
  venv/bin/python harness/job_schema.py && venv/bin/python harness/crew_generic.py --selbsttest
  ```

## Hinweise

- Die eingesetzten „abliterated/uncensored“-Modelle haben keine eingebauten
  Ablehnungen. Das ist eine bewusste Entscheidung für ein privates, lokales
  System — die Verantwortung für die Nutzung liegt beim Betreiber.
- Modell-Lizenzen stehen in den Modelfiles (u. a. Apache-2.0); Ultralytics/YOLOE
  ist AGPL-3.0. Für private Nutzung unkritisch, bei Weiterverbreitung beachten.
- Getestet mit Python 3.12 (venv) und Ollama ≥ 0.12.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
