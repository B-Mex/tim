#!/usr/bin/env python3
"""M1-Zentrale - die Schaltzentrale fuer den lokalen KI-Server.

Was sie ist: eine Bedienoberflaeche, die die Teile dieser Anlage
zusammenfuehrt - Ablaeufe, Modelle, Speicher, Berichte, Telemetrie,
Autonomie-Stufe und Not-Aus. Dazu ein Chat gegen die lokalen Modelle.

Was sie NICHT ist: ein zweiter Ausfuehrungsweg. Alles, was etwas *tut*,
geht durch den Job-Server (m1_job_server.py) und damit durch dessen
Positivliste. Die Zentrale selbst baut keine Befehle und ruft keine
Shell - sie liest Dateien und reicht Aktionen weiter. Wer die Zentrale
uebernimmt, gewinnt dadurch nichts, was der Job-Server nicht ohnehin
erlaubt.

Der Chat ist die einzige Ausnahme und bewusst so gebaut: er spricht
ausschliesslich mit Ollama auf 127.0.0.1 und kann nichts ausfuehren.

Start:
    /opt/ki-server/venv/bin/python m1_zentrale.py

Danach:  http://127.0.0.1:8770
Mobil:   http://<tailscale-ip>:8770   (nur im eigenen Tailnet)

Das Token ist dasselbe wie beim Job-Server (~/.m1_job_token).
"""

import json
import os
import re
import secrets
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# ----------------------------------------------------------------------
# Orte - dieselben wie im Job-Server, bewusst nicht abgeleitet:
# zwei Dienste, die sich gegenseitig importieren, starten nicht mehr
# unabhaengig voneinander.
# ----------------------------------------------------------------------
HOME = Path.home()
DEPLOY_DIR = HOME / "Desktop" / "M1_DEPLOYMENT"
HARNESS_DIR = Path("/opt/ki-server/harness")
JOBS_DIR = HARNESS_DIR / "jobs"
BERICHTE_DIR = DEPLOY_DIR / "berichte"
TELEMETRIE = Path("/opt/ki-server/memory/harness_log.jsonl")
CONFIG_DIR = Path("/opt/ki-server/config")
TOKEN_DATEI = HOME / ".m1_job_token"
OBERFLAECHE = Path(__file__).parent / "zentrale.html"

JOB_SERVER = os.environ.get("M1_JOB_SERVER", "http://127.0.0.1:8765")
OLLAMA = os.environ.get("M1_OLLAMA", "http://127.0.0.1:11434")

# Dieselbe Liste wie autonomie.py und m1_job_server.py. Weicht sie ab,
# meldet die Zentrale "frei", waehrend der Harness laengst gestoppt ist.
STOP_ORTE = [
    Path("/opt/ki-server"),
    HOME / "Desktop" / "M1_DEPLOYMENT",
    Path("/Volumes/M1_DEPLOYMENT"),
    Path("/Volumes/Extreme SSD/M1_DEPLOYMENT"),
    Path("/Volumes/SanDisk/M1_DEPLOYMENT"),
    Path("/Volumes/SANDISK/M1_DEPLOYMENT"),
]

# Ein Lauf mit dem 27B-Modell braucht Minuten.
ZEITGRENZE = 1800

# Laufende und beendete Ablaeufe. Der Browser fragt den Fortschritt ab,
# statt auf eine Antwort zu warten - ein HTTP-Aufruf, der eine halbe
# Stunde offen haengt, ueberlebt keinen Mobilfunkwechsel.
LAEUFE = {}
LAEUFE_SPERRE = threading.Lock()


# ----------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------
def token_holen() -> str:
    """Token lesen, beim ersten Start erzeugen (nur fuer den Besitzer lesbar)."""
    if TOKEN_DATEI.exists():
        wert = TOKEN_DATEI.read_text(encoding="utf-8").strip()
        if wert:
            return wert
    wert = secrets.token_urlsafe(32)
    TOKEN_DATEI.write_text(wert + "\n", encoding="utf-8")
    TOKEN_DATEI.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return wert


TOKEN = token_holen()


def killswitch_aktiv():
    """Pfad der STOP-Datei, wenn eine existiert - sonst None."""
    for ordner in STOP_ORTE:
        ziel = ordner / "STOP"
        try:
            if ziel.exists() or ziel.is_symlink():
                return str(ziel)
        except OSError:
            continue
    return None


def _lies(pfad: Path, grenze: int = 400_000) -> str:
    """Datei lesen, ohne den Dienst an einer Riesendatei aufzuhaengen."""
    try:
        with open(pfad, "r", encoding="utf-8", errors="replace") as f:
            return f.read(grenze)
    except OSError:
        return ""


def _befehl(argumente: list, zeit: int = 20) -> str:
    """Ein festes Kommando ausfuehren - nie mit Eingaben von aussen.

    Die Argumentliste steht in dieser Datei fest verdrahtet. Es gibt
    keinen Weg, von aussen etwas hineinzureichen; deshalb auch kein
    shell=True.
    """
    try:
        fertig = subprocess.run(argumente, capture_output=True, text=True,
                                timeout=zeit)
        return (fertig.stdout or "") + (fertig.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def autonomie_lesen() -> dict:
    """autonomie.conf auswerten - nur lesen, nie schreiben."""
    werte = {}
    text = _lies(CONFIG_DIR / "autonomie.conf", 20_000)
    for zeile in text.splitlines():
        zeile = zeile.split("#", 1)[0].strip()
        if "=" in zeile:
            schluessel, _, wert = zeile.partition("=")
            werte[schluessel.strip()] = wert.strip()
    return werte


def speicher_lage() -> dict:
    """Freier Speicher und die Modellklasse, die der Router daraus waehlt."""
    lage = {"frei_gb": None, "gesamt_gb": None, "klassen": {}}
    try:
        if str(HARNESS_DIR) not in sys.path:
            sys.path.insert(0, str(HARNESS_DIR))
        import psutil                       # noqa: F401  (nur zur Pruefung)
        from model_router import freier_ram_gb, get_model_for_job
        lage["frei_gb"] = round(freier_ram_gb(), 1)
        lage["klassen"] = {str(k): get_model_for_job(k) for k in (1, 2, 3)}
    except Exception as e:
        lage["fehler"] = f"{type(e).__name__}: {e}"
    try:
        import psutil
        lage["gesamt_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:
        pass
    return lage


def ablaeufe_lesen() -> list:
    """Alle Ablaeufe aus jobs/*.json - die Oberflaeche zeichnet daraus den Graph.

    Bewusst datengetrieben: ein neuer Ablauf ist eine JSON-Datei und
    erscheint hier von selbst, ohne dass an der Oberflaeche etwas
    nachgezogen werden muss.
    """
    ergebnis = []
    if not JOBS_DIR.is_dir():
        return ergebnis
    for pfad in sorted(JOBS_DIR.glob("*.json")):
        try:
            job = json.loads(_lies(pfad, 200_000))
        except (json.JSONDecodeError, ValueError) as e:
            ergebnis.append({"name": pfad.stem, "fehlerhaft": str(e)})
            continue
        agenten = []
        for a in job.get("agents", []):
            agenten.append({
                "rolle": a.get("rolle") or a.get("role") or "?",
                "ziel": a.get("ziel") or a.get("goal") or "",
                "werkzeuge": a.get("werkzeuge", []),
                "klasse": a.get("modell_klasse") or a.get("klasse"),
            })
        ergebnis.append({
            "name": job.get("name", pfad.stem),
            "beschreibung": job.get("beschreibung", ""),
            "zeitplan": job.get("schedule_cron") or "",
            "agenten": agenten,
            "aufgaben": len(job.get("tasks", [])),
            "erlaubte_pfade": job.get("erlaubte_pfade", []),
        })
    return ergebnis


def telemetrie_lesen(anzahl: int = 200) -> dict:
    """Die letzten Laeufe und eine kurze Auswertung."""
    zeilen = _lies(TELEMETRIE, 500_000).splitlines()
    eintraege = []
    for z in zeilen[-anzahl:]:
        z = z.strip()
        if not z:
            continue
        try:
            eintraege.append(json.loads(z))
        except ValueError:
            continue
    gesamt = len(eintraege)
    geglueckt = sum(1 for e in eintraege if e.get("ok"))
    dauern = [e.get("dauer_sek", 0) or 0 for e in eintraege if e.get("ok")]
    return {
        "eintraege": list(reversed(eintraege))[:40],
        "gesamt": gesamt,
        "geglueckt": geglueckt,
        "gescheitert": gesamt - geglueckt,
        "dauer_schnitt": round(sum(dauern) / len(dauern), 1) if dauern else 0,
        "kosten_cent": round(sum(e.get("kosten_cent", 0) or 0
                                 for e in eintraege), 2),
    }


def berichte_lesen() -> list:
    """Vorhandene Berichte, neueste zuerst."""
    if not BERICHTE_DIR.is_dir():
        return []
    dateien = []
    for p in BERICHTE_DIR.glob("*.md"):
        try:
            s = p.stat()
        except OSError:
            continue
        dateien.append({
            "name": p.name,
            "groesse": s.st_size,
            "geaendert": datetime.fromtimestamp(s.st_mtime).strftime(
                "%Y-%m-%d %H:%M"),
        })
    return sorted(dateien, key=lambda d: d["geaendert"], reverse=True)


def benchmark_uebersicht() -> dict:
    """Alles Lesende fuer den Benchmark-Reiter, an einer Stelle.

    Bewusst NUR Nachsehen: gemessene Staende, laufender Lauf, Zusatz-
    faelle, passende Berichte. Gestartet wird weiterhin ausschliesslich
    ueber /api/start -> Job-Server-Positivliste - dieser Endpunkt
    verschiebt die Sicherheitslinie nicht.

    Die Logik kommt aus den geprueften Harness-Bausteinen selbst
    (modell_benchmark, benchmark_starter) - kein zweiter Satz Code,
    sonst laufen Bestand und Anzeige auseinander.
    """
    if str(HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(HARNESS_DIR))
    daten = {"stand": [], "laeuft": None, "installiert": [],
             "ungetestet": [], "extra_faelle": [], "extra_meldungen": [],
             "berichte": []}
    try:
        from modell_benchmark import (bisherige_ergebnisse, punktzahl,
                                      installierte_modelle, lade_extra_faelle)
        from benchmark_starter import lauf_laeuft
        bestand = bisherige_ergebnisse()
        for lauf in sorted(bestand.values(), key=punktzahl, reverse=True):
            daten["stand"].append({
                "modell": lauf.get("modell", "?"),
                "punkte": lauf.get("punkte", "?"),
                "tok_pro_s": lauf.get("metrik", {}).get("tok_pro_s"),
                "ladezeit_s": lauf.get("metrik", {}).get("ladezeit_s"),
                "zeit": str(lauf.get("zeit", ""))[:16],
            })
        daten["installiert"] = installierte_modelle()
        daten["ungetestet"] = [m for m in daten["installiert"]
                               if m not in bestand]
        daten["laeuft"] = lauf_laeuft()
        faelle, meldungen = lade_extra_faelle()
        daten["extra_faelle"] = [f["name"] for f in faelle]
        daten["extra_meldungen"] = meldungen
        from benchmark_faelle_pflegen import QUELLEN_DATEI
        try:
            q = json.loads(QUELLEN_DATEI.read_text(encoding="utf-8"))
            if isinstance(q.get("quellen"), list):
                daten["quellen"] = [
                    {"name": str(e.get("name", "")),
                     "url": str(e.get("url", "")),
                     "hinweis": str(e.get("hinweis", ""))}
                    for e in q["quellen"] if isinstance(e, dict)]
        except (OSError, ValueError):
            pass
    except Exception as e:  # Anzeige darf nie die Zentrale reissen
        daten["fehler"] = f"{type(e).__name__}: {e}"
    daten.setdefault("quellen", [])
    daten["berichte"] = [b for b in berichte_lesen()
                         if b["name"].startswith("modell_benchmark")
                         or b["name"].startswith("modell_scan")
                         or b["name"].startswith("modelle_")]
    return daten


# Bits pro Gewicht, grob. Bei gleicher Parameterzahl entscheidet die
# Quantisierung ueber die Qualitaet: Q5_K_M haelt spuerbar mehr vom
# Original als IQ3_M.
QUANT_RANG = {
    "Q8_0": 90, "Q6_K": 80, "Q5_K_M": 70, "Q5_K_S": 68, "Q5_0": 65,
    "Q4_K_M": 60, "Q4_K_S": 58, "Q4_0": 55, "IQ4_XS": 52, "IQ4_NL": 51,
    "Q3_K_L": 45, "Q3_K_M": 43, "Q3_K_S": 41, "IQ3_M": 38, "IQ3_XS": 36,
    "Q2_K": 25, "IQ2_M": 22,
}


def _parameter_zahl(text: str) -> float:
    """'42.4B' -> 42.4, '3.2B' -> 3.2. Unbekanntes zaehlt als 0."""
    try:
        return float(str(text).upper().rstrip("B").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def modelle_lesen() -> list:
    """Installierte Ollama-Modelle, nach Staerke sortiert.

    Sortiert wird nach Parameterzahl, bei Gleichstand nach Quantisierung -
    beides Angaben, die Ollama selbst liefert. Damit stimmt die Reihenfolge
    auch fuer Modelle, die es heute noch gar nicht gibt; eine gepflegte
    Rangliste im Code waere nach dem naechsten Import veraltet.
    """
    modelle = []
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=8) as antwort:
            daten = json.loads(antwort.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return modelle

    for m in daten.get("models", []):
        name = m.get("name", "?")
        eintrag = {
            "name": name,
            "groesse_gb": round((m.get("size") or 0) / 1e9, 1),
            "geaendert": (m.get("modified_at") or "")[:16].replace("T", " "),
            "parameter": "", "quant": "", "kontext": None, "kann": [],
        }
        try:
            a = urllib.request.Request(
                OLLAMA + "/api/show",
                data=json.dumps({"model": name}).encode("utf-8"), method="POST")
            with urllib.request.urlopen(a, timeout=10) as antwort:
                d = json.loads(antwort.read().decode("utf-8"))
            det = d.get("details", {}) or {}
            info = d.get("model_info", {}) or {}
            eintrag["parameter"] = det.get("parameter_size", "")
            eintrag["quant"] = det.get("quantization_level", "")
            eintrag["kann"] = sorted(d.get("capabilities", []) or [])
            eintrag["kontext"] = next(
                (v for k, v in info.items() if k.endswith("context_length")), None)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        eintrag["_rang"] = (_parameter_zahl(eintrag["parameter"]),
                            QUANT_RANG.get(eintrag["quant"], 0))
        modelle.append(eintrag)

    modelle.sort(key=lambda m: m["_rang"], reverse=True)
    for m in modelle:
        m.pop("_rang", None)
    return modelle


def dienste_pruefen() -> list:
    """Erreichbarkeit der Nachbardienste - eine Zeile pro Baustein."""
    ziele = [
        ("Ollama", OLLAMA + "/api/tags"),
        ("Job-Server", JOB_SERVER + "/aktionen"),
        ("SearXNG", "http://127.0.0.1:8888/"),
        ("Open WebUI", "http://127.0.0.1:8080/"),
        ("Odysseus", "http://127.0.0.1:7000/"),
        ("ChromaDB", "http://127.0.0.1:8100/api/v2/heartbeat"),
        ("Tims Auge", KAMERA + "/messung"),
    ]
    ergebnis = []
    for name, url in ziele:
        anfrage = urllib.request.Request(url)
        if "8765" in url:
            anfrage.add_header("X-M1-Token", TOKEN)
        zustand = "weg"
        try:
            with urllib.request.urlopen(anfrage, timeout=3) as antwort:
                zustand = "da" if antwort.status < 500 else "gestoert"
        except urllib.error.HTTPError as e:
            # 401/403 heisst: der Dienst antwortet, nur nicht auf diese Anfrage.
            zustand = "da" if e.code < 500 else "gestoert"
        except (urllib.error.URLError, OSError):
            zustand = "weg"
        ergebnis.append({"name": name, "zustand": zustand})
    return ergebnis


# ----------------------------------------------------------------------
# Tims Auge - der Kameradienst
#
# Der Kameradienst selbst hoert nur auf 127.0.0.1; das Bild aus der
# Wohnung soll nicht im Netz haengen. Damit es trotzdem in Tim zu sehen
# ist - auch vom Handy aus, das ueber Tailscale kommt - reicht die
# Zentrale es durch. Sie ist die einzige Stelle, die von aussen
# erreichbar ist, und sie verlangt dafuer dasselbe Token wie fuer alles
# andere. Ohne diesen Umweg muesste der Kameradienst selbst ins Netz
# gebunden werden, und genau das soll er nicht.
#
# Weitergereicht wird nur lesend. Geschaltet wird ueber /auge, und auch
# das kann nur an- und ausmachen, was der Kameradienst ohnehin anbietet.
# ----------------------------------------------------------------------
KAMERA = os.environ.get("M1_KAMERA", "http://127.0.0.1:8781")

# Der Befehl, mit dem Mexla den Kameradienst startet. Er steht hier, damit
# die Oberflaeche ihn zum Kopieren anzeigen kann: Starten laesst sich der
# Dienst nur von Hand im Terminal, weil die Kameraerlaubnis unter macOS
# am zugreifenden Programm haengt und nicht am Skript (dieselbe Falle wie
# damals bei Bluetooth). Ein Start ueber launchd oder den Job-Server
# scheitert daran - der Dienst liefe, saehe aber nichts.
KAMERA_BEFEHL = (
    "/opt/ki-server/venv/bin/python "
    "~/Desktop/M1_DEPLOYMENT/hardware/kamera/kamera_dienst.py")


def _kamera_holen(pfad: str, zeit: int = 8):
    """Etwas beim Kameradienst abholen. Gibt (Daten, Typ) oder (None, Fehler)."""
    try:
        with urllib.request.urlopen(KAMERA + pfad, timeout=zeit) as antwort:
            return antwort.read(), antwort.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        return None, str(fehler)


def auge_zustand() -> dict:
    """Was das Auge gerade tut - und ob es ueberhaupt da ist."""
    roh, typ = _kamera_holen("/auge")
    if roh is None:
        return {
            "da": False,
            "an": False,
            "grund": "Der Kameradienst antwortet nicht.",
            "befehl": KAMERA_BEFEHL,
            "adresse": KAMERA,
        }
    try:
        zustand = json.loads(roh.decode("utf-8"))
    except ValueError:
        return {"da": False, "an": False, "grund": "unverstaendliche Antwort",
                "befehl": KAMERA_BEFEHL, "adresse": KAMERA}
    zustand["da"] = True
    zustand["befehl"] = KAMERA_BEFEHL
    zustand["adresse"] = KAMERA
    return zustand


# Die Seite fuer "In eigenem Fenster oeffnen". Das Token steht in der
# Adresse (ein Fensterlink kann keinen Kopf mitschicken); die Seite
# selbst traegt es nicht im HTML, sondern liest es aus der eigenen
# Adresse und nutzt es fuer Bild und Fundliste.
AUGE_FENSTER = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tims Auge</title><style>
body{background:#14161a;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
margin:0;padding:14px}
h1{font-size:16px;font-weight:600;margin:0 0 10px}
h1 span{color:#e8873a}
#bild{max-width:100%;border-radius:10px;display:block;background:#000}
#funde{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.fund{background:#1d2026;border:1px solid #2c313c;border-radius:7px;
padding:5px 10px;font-size:13px}
.fund b{font-weight:600}
.leise{color:#9aa0ab;font-size:12.5px;margin-top:8px}
</style></head><body>
<h1>Ti<span>m</span>s Auge</h1>
<img id="bild" alt="Livebild">
<div id="messung" style="margin-top:10px;display:flex;gap:10px;align-items:center">
  <div id="klecks" style="width:38px;height:38px;border-radius:8px;border:1px solid #444"></div>
  <div><div id="farbname" style="font-size:19px;font-weight:600">&mdash;</div>
    <div id="farbzahlen" class="leise" style="margin-top:0;font-family:ui-monospace,Menlo,monospace"></div></div>
</div>
<div id="funde"></div>
<div class="leise" id="stand"></div>
<script>
const token = new URLSearchParams(location.search).get("token") || "";
function stromVerbinden() {
  document.getElementById("bild").src =
    "/api/auge/strom?token=" + encodeURIComponent(token) + "&neu=" + Date.now();
}
stromVerbinden();
/* Ein MJPEG-Strom wird nie uebersprungen: Liegt das Fenster laenger im
   Hintergrund, laeuft er auf und hinkt danach dauerhaft hinterher.
   Beim Sichtbarwerden deshalb frisch verbinden - der Rueckstand ist weg. */
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) stromVerbinden();
});
async function messen() {
  try {
    const a = await fetch("/api/auge/messung", {headers: {"X-M1-Token": token}});
    if (!a.ok) return;
    const m = await a.json();
    if (m.fehler) { document.getElementById("farbname").textContent = m.fehler; return; }
    document.getElementById("farbname").textContent = m.name;
    document.getElementById("klecks").style.background = m.hex;
    document.getElementById("farbzahlen").textContent =
      `RGB ${m.rot|0} ${m.gruen|0} ${m.blau|0} · Farbton ${m.farbton}° · ` +
      `Sättigung ${m.saettigung} · Helligkeit ${m.helligkeit}`;
  } catch (e) {}
}
async function nachziehen() {
  try {
    const a = await fetch("/api/auge", {headers: {"X-M1-Token": token}});
    if (!a.ok) return;
    const d = await a.json();
    const funde = d.gesehen || [];
    document.getElementById("funde").innerHTML = funde.map(f =>
      `<span class="fund"><b>${f.deutsch || f.name}</b> ${
        Math.round(f.vertrauen * 100)}%</span>`).join("");
    document.getElementById("stand").textContent = d.an
      ? (funde.length ? "" : "Auge an — noch nichts Beständiges gesehen.")
      : "Auge ist aus — nur das Livebild läuft.";
  } catch (e) {}
}
nachziehen(); messen(); setInterval(nachziehen, 2000); setInterval(messen, 700);
</script></body></html>"""


def auge_fuer_chat(zustand=None) -> str:
    """Der Auge-Stand als Klartext fuer die Rollenanweisung des Chats.

    Der Chat bleibt werkzeuglos - aber die Zentrale darf dem Modell
    Fakten VORLEGEN. Ohne diesen Block behauptete Tim am 23.08.2026
    wortreich, er habe "kein physisches Auge" - waehrend nebenan die
    Kamera lief. Ein Modell weiss nur, was in seinen Nachrichten steht.
    """
    if zustand is None:
        # Kurzer Timeout: Eine Chat-Antwort darf nicht an einer
        # haengenden Kamera kleben.
        roh, _ = _kamera_holen("/auge", zeit=2)
        if roh is None:
            return ("TIMS AUGE: Der Kameradienst ist gerade nicht "
                    "erreichbar. Es gibt ihn aber - eine Webcam am Mac, "
                    "bedienbar im Reiter 'Auge'.")
        try:
            zustand = json.loads(roh.decode("utf-8"))
        except ValueError:
            return "TIMS AUGE: Der Kameradienst antwortet unverstaendlich."
    if not zustand.get("an"):
        return ("TIMS AUGE: Du hast ein Auge (Webcam am Mac, Reiter "
                "'Auge'), aber es ist gerade AUSGESCHALTET - du siehst "
                "im Moment nichts. Einschalten kann Mexla im Reiter 'Auge'.")
    funde = zustand.get("gesehen") or []
    if funde:
        was = ", ".join("%s (%d%%)" % (f.get("deutsch") or f.get("name"),
                                       round(f.get("vertrauen", 0) * 100))
                        for f in funde[:8])
    else:
        was = "noch nichts Bestaendiges"
    return ("TIMS AUGE: Du hast ein Auge - eine Webcam am Mac mit "
            "Objekterkennung (Reiter 'Auge'). Es ist AN. Gerade erkannt: "
            + was + ". Das ist dein echter, aktueller Blick ins Zimmer - "
            "nutze ihn, wenn Mexla fragt, was du siehst.")


def auge_schalten(an: bool) -> dict:
    """Die Objekterkennung im Kameradienst an- oder ausschalten."""
    roh, _ = _kamera_holen("/auge?an=%d" % (1 if an else 0), zeit=15)
    if roh is None:
        return auge_zustand()
    try:
        zustand = json.loads(roh.decode("utf-8"))
    except ValueError:
        return auge_zustand()
    zustand["da"] = True
    zustand["befehl"] = KAMERA_BEFEHL
    zustand["adresse"] = KAMERA
    return zustand


# ----------------------------------------------------------------------
# Ablaeufe starten - immer ueber den Job-Server
# ----------------------------------------------------------------------
def job_server_aktionen() -> dict:
    """Die Positivliste des Job-Servers abfragen."""
    anfrage = urllib.request.Request(JOB_SERVER + "/aktionen")
    anfrage.add_header("X-M1-Token", TOKEN)
    try:
        with urllib.request.urlopen(anfrage, timeout=5) as antwort:
            return json.loads(antwort.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"fehler": f"Job-Server nicht erreichbar: {e}"}


def _lauf_ausfuehren(lauf_id: str, aktion: str, argument: str) -> None:
    """Im Hintergrund: Aktion an den Job-Server geben, Ergebnis ablegen."""
    nutzlast = json.dumps({"aktion": aktion, "argument": argument}).encode("utf-8")
    anfrage = urllib.request.Request(JOB_SERVER + "/start", data=nutzlast,
                                     method="POST")
    anfrage.add_header("X-M1-Token", TOKEN)
    anfrage.add_header("Content-Type", "application/json")
    text, code = "", 0
    try:
        with urllib.request.urlopen(anfrage, timeout=ZEITGRENZE) as antwort:
            roh = json.loads(antwort.read().decode("utf-8"))
            text = roh.get("ausgabe") or roh.get("fehler") or json.dumps(
                roh, ensure_ascii=False, indent=2)
            code = roh.get("code", 0)
    except urllib.error.HTTPError as e:
        try:
            roh = json.loads(e.read().decode("utf-8"))
            text = roh.get("fehler") or str(roh)
        except Exception:
            text = f"HTTP {e.code}"
        code = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        text = (f"Job-Server nicht erreichbar: {e}\n\n"
                f"Starten mit:\n"
                f"  /opt/ki-server/venv/bin/python "
                f"/opt/ki-server/oberflaeche/m1_job_server.py")
        code = -1

    with LAEUFE_SPERRE:
        LAEUFE[lauf_id].update({
            "fertig": True,
            "ausgabe": text,
            "code": code,
            "ende": time.time(),
        })


def lauf_starten(aktion: str, argument: str) -> str:
    """Startet eine Aktion und gibt sofort eine Lauf-Nummer zurueck."""
    lauf_id = uuid.uuid4().hex[:12]
    with LAEUFE_SPERRE:
        # Alte Laeufe aufraeumen, damit der Speicher nicht unbegrenzt waechst.
        if len(LAEUFE) > 50:
            aeltest = sorted(LAEUFE.items(), key=lambda kv: kv[1]["start"])
            for k, _ in aeltest[:20]:
                LAEUFE.pop(k, None)
        LAEUFE[lauf_id] = {
            "aktion": aktion,
            "argument": argument,
            "start": time.time(),
            "fertig": False,
            "ausgabe": "",
            "code": None,
        }
    threading.Thread(target=_lauf_ausfuehren,
                     args=(lauf_id, aktion, argument), daemon=True).start()
    return lauf_id


# ----------------------------------------------------------------------
# Gespraechsverlauf - damit Tim sich erinnert
# ----------------------------------------------------------------------
# Der Verlauf lag bisher nur im Browser und war nach jedem Neuladen weg.
# Jetzt liegt er auf dem Mac: am Handy angefangene Gespraeche stehen
# damit auch am Rechner - und umgekehrt.
VERLAUF_DATEI = Path("/opt/ki-server/memory/tim_verlauf.jsonl")

# ----------------------------------------------------------------------
# Unterhaltungen (seit 23.08.2026)
#
# Vorher gab es genau EINEN fortlaufenden Verlauf - "leeren" warf alles
# weg, und ein aelteres Gespraech war unauffindbar. Jetzt ist jede
# Unterhaltung eine eigene Datei unter memory/chats/, die Oberflaeche
# zeigt sie als Liste neben dem Chat.
#
# Die Kennung ist streng begrenzt (Buchstaben, Ziffern, - und _), damit
# aus einer Chat-ID nie ein Pfad werden kann. Der Sprachassistent und
# alte Clients ohne Kennung landen in der Unterhaltung "standard".
# Kamerabilder aus dem Chat werden als Datei festgehalten - ein alter
# Chat soll die damalige Aufnahme zeigen, nicht das heutige Livebild.
# ----------------------------------------------------------------------
CHATS_DIR = Path("/opt/ki-server/memory/chats")
CHATBILDER_DIR = CHATS_DIR / "bilder"
CHAT_ID_MUSTER = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
CHATBILD_MUSTER = re.compile(r"^kb_[0-9]{8}_[0-9]{6}\.jpg$")


def _chat_datei(chat):
    """Die Datei zu einer Chat-Kennung - None bei unzulaessiger Kennung."""
    if not CHAT_ID_MUSTER.match(chat or ""):
        return None
    return CHATS_DIR / (chat + ".jsonl")


def chats_migrieren():
    """Den alten Ein-Datei-Verlauf einmalig als Unterhaltung uebernehmen."""
    try:
        CHATS_DIR.mkdir(parents=True, exist_ok=True)
        if VERLAUF_DATEI.exists():
            ziel = CHATS_DIR / "altbestand.jsonl"
            if not ziel.exists():
                VERLAUF_DATEI.rename(ziel)
    except OSError:
        pass


def chats_auflisten():
    """Alle Unterhaltungen, juengste zuerst - Titel aus der ersten Frage."""
    chats_migrieren()
    raus = []
    try:
        dateien = sorted(CHATS_DIR.glob("*.jsonl"),
                         key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return raus
    for datei in dateien:
        titel = ""
        anzahl = 0
        for zeile in _lies(datei, 400_000).splitlines():
            try:
                n = json.loads(zeile)
            except ValueError:
                continue
            anzahl += 1
            if not titel and n.get("role") == "user":
                titel = " ".join(str(n.get("content", "")).split())[:60]
        raus.append({
            "id": datei.stem,
            "titel": titel or "(leer)",
            "anzahl": anzahl,
            "zuletzt": datetime.fromtimestamp(
                datei.stat().st_mtime).strftime("%d.%m. %H:%M"),
        })
    return raus


def chatbild_sichern():
    """Das aktuelle Kamerabild (mit Einblendungen) dauerhaft ablegen.

    Gibt den Dateinamen zurueck, oder None. Der Chat zeigt damit auch
    spaeter die DAMALIGE Aufnahme - nicht das heutige Livebild."""
    roh, _typ = _kamera_holen("/bild.jpg")
    if roh is None:
        return None
    name = "kb_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".jpg"
    try:
        CHATBILDER_DIR.mkdir(parents=True, exist_ok=True)
        (CHATBILDER_DIR / name).write_bytes(roh)
    except OSError:
        return None
    return name
VERLAUF_GRENZE = 400          # so viele Nachrichten werden aufgehoben


def verlauf_anhaengen(rolle: str, inhalt: str, modell: str = "",
                      chat: str = "standard", zusatz: dict = None) -> None:
    datei = _chat_datei(chat)
    if datei is None:
        return
    try:
        datei.parent.mkdir(parents=True, exist_ok=True)
        eintrag = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": rolle, "content": inhalt, "modell": modell,
        }
        if zusatz:
            eintrag.update(zusatz)
        with open(datei, "a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    except OSError:
        pass


def verlauf_lesen(anzahl: int = 60, chat: str = "standard") -> list:
    datei = _chat_datei(chat)
    if datei is None:
        return []
    zeilen = _lies(datei, 2_000_000).splitlines()
    raus = []
    for z in zeilen[-anzahl:]:
        z = z.strip()
        if not z:
            continue
        try:
            raus.append(json.loads(z))
        except ValueError:
            continue
    return raus


def verlauf_leeren(chat: str = "standard") -> None:
    datei = _chat_datei(chat)
    if datei is None:
        return
    try:
        datei.unlink(missing_ok=True)
    except OSError:
        pass


def verlauf_kuerzen(chat: str = "standard") -> None:
    """Aeltestes abschneiden, damit die Datei nicht unbegrenzt waechst."""
    datei = _chat_datei(chat)
    if datei is None:
        return
    try:
        if not datei.exists():
            return
        zeilen = datei.read_text(encoding="utf-8").splitlines()
        if len(zeilen) > VERLAUF_GRENZE:
            datei.write_text("\n".join(zeilen[-VERLAUF_GRENZE:]) + "\n",
                                     encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------
# Shell - nur wenn autonomie.conf sie ausdruecklich freigibt
# ----------------------------------------------------------------------
# Das ist der einzige Ort in dieser Datei, an dem ein Befehl von aussen
# ausgefuehrt wird. Deshalb haengt er an einem eigenen Schalter
# (ERLAUBE_SHELL), ist im Auslieferungszustand aus, und jeder Aufruf wird
# protokolliert - auch der abgelehnte.
SHELL_PROTOKOLL = Path("/opt/ki-server/memory/tim_shell.jsonl")
SHELL_ZEITGRENZE = 120
SHELL_AUSGABE_GRENZE = 200_000
SHELL_ARBEITSORDNER = HOME / "Desktop" / "Quick Agent Projekte"


def shell_erlaubt() -> tuple:
    """(erlaubt, grund) - fragt dieselbe Stelle wie der Harness."""
    stop = killswitch_aktiv()
    if stop:
        return False, f"Kill-Switch aktiv ({stop})"
    try:
        if str(HARNESS_DIR) not in sys.path:
            sys.path.insert(0, str(HARNESS_DIR))
        from autonomie import pruefe_aktion
        return pruefe_aktion("shell")
    except ImportError as e:
        return False, f"autonomie.py nicht ladbar: {e}"


def shell_protokoll_schreiben(eintrag: dict) -> None:
    try:
        SHELL_PROTOKOLL.parent.mkdir(parents=True, exist_ok=True)
        with open(SHELL_PROTOKOLL, "a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    except OSError:
        pass


def shell_ausfuehren(befehl: str, ordner: str = "") -> dict:
    erlaubt, grund = shell_erlaubt()
    eintrag = {"ts": datetime.now().isoformat(timespec="seconds"),
               "befehl": befehl, "ordner": ordner}
    if not erlaubt:
        eintrag.update({"abgelehnt": grund})
        shell_protokoll_schreiben(eintrag)
        return {"fehler": grund, "abgelehnt": True}

    arbeitsordner = Path(ordner).expanduser() if ordner else SHELL_ARBEITSORDNER
    if not arbeitsordner.is_dir():
        arbeitsordner = HOME

    start = time.time()
    try:
        fertig = subprocess.run(
            befehl, shell=True, capture_output=True, text=True,
            timeout=SHELL_ZEITGRENZE, cwd=str(arbeitsordner),
            env={**os.environ,
                 "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")})
        ausgabe = (fertig.stdout or "") + (fertig.stderr or "")
        code = fertig.returncode
    except subprocess.TimeoutExpired:
        ausgabe, code = f"Abgebrochen nach {SHELL_ZEITGRENZE} s.", -1
    except (OSError, subprocess.SubprocessError) as e:
        ausgabe, code = f"{type(e).__name__}: {e}", -1

    if len(ausgabe) > SHELL_AUSGABE_GRENZE:
        ausgabe = ausgabe[:SHELL_AUSGABE_GRENZE] + "\n[... gekuerzt]"

    eintrag.update({"code": code, "dauer_sek": round(time.time() - start, 1),
                    "ausgabe": ausgabe[:4000]})
    shell_protokoll_schreiben(eintrag)
    return {"ausgabe": ausgabe, "code": code,
            "ordner": str(arbeitsordner),
            "dauer_sek": round(time.time() - start, 1)}


def shell_protokoll_lesen(anzahl: int = 50) -> list:
    zeilen = _lies(SHELL_PROTOKOLL, 1_000_000).splitlines()
    raus = []
    for z in zeilen[-anzahl:]:
        z = z.strip()
        if not z:
            continue
        try:
            raus.append(json.loads(z))
        except ValueError:
            continue
    return list(reversed(raus))


# ----------------------------------------------------------------------
# Orchestrator - waehlt das Modell nach Aufgabenart
# ----------------------------------------------------------------------
# Bewusst ohne eigenen Modellaufruf: ein Modell zu laden, nur um zu
# entscheiden, welches Modell geladen wird, kostet die Ladezeit zweimal.
# Dieselbe Ueberlegung wie beim Quality Gate, das ohne KI auskommt.
#
# Die Schluesselwoerter sind die Aufgabenarten aus VOLLAUSBAU_SYSTEM.md:
# Code, Werkzeuge, Denken, Kurzes.
AUFGABEN = [
    ("code", ["code", "python", "javascript", "def ", "bug",
              "fehler im code", "kompilier", "syntax", "refactor", "script",
              "programmier", "micropython", "gpio", "arduino", "pico",
              "debug", "stacktrace", "regex", "sql", "json", "schreib mir ein",
              "implementier", "funktionsaufruf"]),
    ("werkzeuge", ["such", "recherch", "im netz", "website", "quelle",
                   "finde heraus", "aktuell", "preis", "vergleich",
                   "datenblatt", "bestell"]),
    ("denken", ["plan", "konzept", "architektur", "abwäg", "abwaeg",
                "entscheid", "strategie", "warum", "begründ", "begruend",
                "durchdenk", "vor- und nachteil", "alternativ"]),
]

# Aufgabenart -> bevorzugtes Modell (Namensteil, damit :latest egal ist)
#
# Stand 23.08.2026, auf dieser Maschine gemessen statt geschaetzt
# (harness/modell_benchmark.py, 14 Pruefungen; Bericht:
# berichte/modell_benchmark_2026-08-23_korrigiert.md):
#   qwen3.6:35b-a3b 14/14, 42.6 Tok/s, 11.3 s Ladezeit - stark UND schnell,
#                   braucht das iogpu-Limit (LaunchDaemon com.mexla.iogpu-limit)
#   qwen3.5:9b      14/14, 30.5 Tok/s,  4.8 s Ladezeit - klein und ehrlich
#   qwen3.8:27b     14/14, 13.0 Tok/s - gruendlich, aber zaeh; bleibt Reserve
# gpt-oss:20b steht bewusst nicht mehr hier: im Benchmark lieferte es
# zweimal nach minutenlangem Denken eine LEERE Antwort - am Sprachweg
# waere das Schweigen. llama-fast flog schon am 21.08. (erfundene
# Zeilenzahl); im Benchmark erfand es zusaetzlich zwei Bundespraesidenten.
AUFGABE_MODELL = {
    "code": "qwen3.6:35b",
    "werkzeuge": "qwen3.5:9b",
    "denken": "qwen3.6:35b",
    "kurz": "qwen3.5:9b",
}

# Wenn die Aufgabenart nichts Bestimmtes ergibt, nimmt der Orchestrator
# dieses Modell - nicht mehr blind das staerkste. Das staerkste ist hier
# ein 23-GB-Modell, das fuer eine kurze Frage erst 11 s laedt.
STANDARD_MODELL = "qwen3.5:9b"

# Ab wann eine Frage als "kurz" gilt und das kleine Modell reicht.
KURZ_ZEICHEN = 80


def geladene_modelle() -> list:
    """Was Ollama gerade im Speicher haelt - ein Wechsel kostet 10-30 s."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as antwort:
            d = json.loads(antwort.read().decode("utf-8"))
        return [m.get("name", "") for m in d.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def orchestrator(frage: str, modelle: list = None) -> dict:
    """Waehlt ein Modell und begruendet die Wahl.

    Gibt immer eine Begruendung zurueck, damit in der Oberflaeche
    nachvollziehbar bleibt, warum gerade dieses Modell antwortet.
    """
    modelle = modelle if modelle is not None else modelle_lesen()
    if not modelle:
        return {"modell": "", "grund": "keine Modelle gefunden"}

    namen = [m["name"] for m in modelle]

    def finde(teil):
        return next((n for n in namen if n.startswith(teil)), None)

    text = (frage or "").lower()

    # 1. Aufgabenart bestimmen
    art = "allgemein"
    if len(text) <= KURZ_ZEICHEN and "?" in text:
        art = "kurz"
    for name, woerter in AUFGABEN:
        if any(w in text for w in woerter):
            art = name
            break

    wunsch = finde(AUFGABE_MODELL.get(art, "")) if art in AUFGABE_MODELL else None
    # Fuer alles andere das Standardmodell. Frueher stand hier "das
    # staerkste" - das ist auf dieser Maschine ein dichtes 42B-Modell,
    # das fuer eine simple Frage 23 s laedt und dann 8 Token/s liefert.
    if not wunsch:
        wunsch = finde(STANDARD_MODELL) or namen[0]

    # 2. Passt es ueberhaupt auf diese Maschine?
    #
    # Bewusst NICHT gegen den gerade freien Speicher: macOS gibt Seiten ab,
    # sobald Ollama laedt, und bei laufendem Docker und Browser sind selten
    # 18 GB "frei". Eine Pruefung gegen den Moment-Wert schickt jede Frage
    # zum kleinsten Modell (gemessen am 21.08.2026).
    #
    # Die harte Grenze ist der Metal-Speicher. Standard: macOS gibt der GPU
    # rund 68 % des Arbeitsspeichers. Seit 23.08.2026 hebt der LaunchDaemon
    # com.mexla.iogpu-limit das Limit aber beim Start an, damit
    # qwen3.6:35b-a3b (23.9 GB) komplett auf die GPU passt - deshalb das
    # echte Limit beim System erfragen statt es zu schaetzen.
    lage = speicher_lage()
    gesamt = lage.get("gesamt_gb")
    gewaehlt, grund = wunsch, f"Aufgabenart: {art}"
    if gesamt:
        grenze = gesamt * 0.68
        try:
            wired_mb = int(subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "iogpu.wired_limit_mb"],
                timeout=3).strip())
            if wired_mb > 0:
                grenze = wired_mb * 1024 * 1024 / 1e9  # MiB -> dezimale GB
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # sysctl nicht lesbar -> beim 68-%-Standard bleiben
        noetig = next((m["groesse_gb"] for m in modelle if m["name"] == wunsch), 0)
        if noetig and noetig > grenze:
            klein = finde(STANDARD_MODELL) or namen[-1]
            grund = (f"{wunsch} braucht {noetig} GB, auf die GPU passen nur "
                     f"{grenze:.0f} GB - deshalb {klein}")
            return {"modell": klein, "grund": grund, "art": art}

    # 3. Modellwechsel vermeiden, wenn ein passendes schon geladen ist.
    geladen = geladene_modelle()
    if geladen and gewaehlt not in geladen:
        for g in geladen:
            # Nur tauschen, wenn das geladene fuer dieselbe Art taugt -
            # sonst waere Tempo wichtiger als Eignung.
            if art in AUFGABE_MODELL and g.startswith(AUFGABE_MODELL[art]):
                return {"modell": g, "art": art,
                        "grund": f"Aufgabenart: {art}, {g} ist bereits geladen"}
        grund += f" (Wechsel von {geladen[0]}, kostet Ladezeit)"

    return {"modell": gewaehlt, "grund": grund, "art": art}


# ----------------------------------------------------------------------
# Sprachassistent - Zustand und Protokoll fuer die Metriken-Ansicht
# ----------------------------------------------------------------------
SPRACHLOG_DATEI = Path("/opt/ki-server/logs/sprachassistent.log")
SPRACH_DIENST = "com.ki-server.sprachassistent"


def sprachassistent_zustand() -> dict:
    """Laeuft der Sprachassistent, und was hat er zuletzt gehoert?

    Nur lesend: gestartet und gestoppt wird er - wie alles - ueber den
    Job-Server und dessen Positivliste, nie von der Zentrale selbst.
    """
    lauf = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{SPRACH_DIENST}"],
        capture_output=True, text=True)
    laeuft = lauf.returncode == 0 and "state = running" in lauf.stdout

    zeilen: list[str] = []
    try:
        with open(SPRACHLOG_DATEI, "rb") as f:
            f.seek(0, os.SEEK_END)
            groesse = f.tell()
            # Nur das Ende lesen - das Protokoll waechst im Dauerbetrieb.
            f.seek(max(0, groesse - 32768))
            roh = f.read().decode("utf-8", "replace")
        zeilen = roh.splitlines()[-80:]
    except OSError:
        pass
    return {"laeuft": laeuft, "zeilen": zeilen}


# ----------------------------------------------------------------------
# Zuhoeren - Sprache zu Text, ausschliesslich lokal
# ----------------------------------------------------------------------
# Bewusst nicht die Spracherkennung des Browsers: Safari und Chrome
# schicken die Aufnahme dafuer an ihre eigenen Server. Hier laeuft
# whisper.cpp auf diesem Mac - die Aufnahme verlaesst das Haus nicht.
WHISPER_BIN = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
WHISPER_MODELL = Path("/opt/ki-server/whisper-models/ggml-medium.bin")
# medium statt base: hier zaehlt Genauigkeit, nicht Reaktionszeit - anders
# als beim Weckwort, das im Sekundentakt geprueft wird.
WHISPER_RUECKFALL = Path("/opt/ki-server/whisper-models/ggml-base.bin")
AUFNAHME_GRENZE = 20_000_000     # rund 10 Minuten bei 16 kHz Mono


def hoeren(wav: bytes) -> dict:
    """Eine WAV-Aufnahme in Text umwandeln.

    Der Browser liefert bereits 16-kHz-Mono-WAV. Das ist Absicht: ohne
    ffmpeg auf diesem Mac koennte der Server ein WebM/Opus des Browsers
    gar nicht oeffnen, und Software nachinstallieren ist laut
    autonomie.conf nicht erlaubt.
    """
    if not WHISPER_BIN:
        return {"fehler": "whisper-cli nicht gefunden (brew install whisper-cpp)"}
    modell = WHISPER_MODELL if WHISPER_MODELL.exists() else WHISPER_RUECKFALL
    if not modell.exists():
        return {"fehler": f"kein Whisper-Modell unter {modell.parent}"}
    if not wav.startswith(b"RIFF"):
        return {"fehler": "keine WAV-Daten empfangen"}

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
        pfad = f.name
    try:
        fertig = subprocess.run(
            [WHISPER_BIN, "-m", str(modell), "-l", "de", "-np", "-nt",
             "-f", pfad],
            capture_output=True, text=True, timeout=300)
        text = (fertig.stdout or "").strip()
        # Whisper setzt Platzhalter wie [MUSIK] oder (Applaus), wenn es
        # nichts Sprachliches findet. Die gehoeren nicht in den Chat.
        text = re.sub(r"[\[(][^\])]{0,40}[\])]", "", text).strip()
        if not text:
            return {"text": "", "hinweis": "nichts verstanden"}
        return {"text": text, "modell": modell.name}
    except subprocess.TimeoutExpired:
        return {"fehler": "Erkennung hat zu lange gebraucht"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"fehler": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.unlink(pfad)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Chat - spricht nur mit Ollama, kann nichts ausfuehren
# ----------------------------------------------------------------------
# Ohne Rollenanweisung erfindet sich ein Modell seine Rolle selbst. Am
# 21.08.2026 behauptete Tim daraufhin, Skripte gebaut, oh-my-zsh
# installiert, PDFs erzeugt und Slack-Nachrichten verschickt zu haben -
# alles frei erfunden, inklusive GitHub-Links und falschem Geraet.
# Bei abliterierten Modellen ist das kein Randfall: gerade die Schichten,
# die sonst "das kann ich nicht" sagen lassen, sind dort abgeschwaecht.
SYSTEM_PROMPT = """Du bist Tim, die Bedienoberflaeche von Mexlas lokalem
KI-Server. Du laeufst auf einem Mac Studio M1 Max mit 32 GB unter macOS
Sequoia. Du sprichst Deutsch.

WAS DU IN DIESEM CHAT KANNST:
Reden, erklaeren, Fragen beantworten, Vorschlaege machen, Code aufschreiben.
Dazu zwei Werkzeuge, beide nur LESEND:
- websuche: sucht im Netz (ueber das lokale SearXNG, kein Tracking).
  Nutze es ungefragt, wenn eine Frage aktuelles Wissen braucht -
  Preise, Verfuegbarkeit, Neuigkeiten, Datenblaetter, Versionen - oder
  wenn du unsicher bist. Erfinde niemals Links oder Zahlen, die du
  nicht gesucht hast.
- webseite_lesen: holt den Text einer Seite, wenn die Suchtreffer nicht
  reichen. Nur oeffentliche Adressen; innere Dienste sind gesperrt.
Nenne die Quelle (Adresse), wenn du etwas Gesuchtes wiedergibst.

WAS DU AUSFUEHREN KANNST (seit 23.08.2026):
- aktion_starten: genau EINE Aktion aus der festen Positivliste des
  Job-Servers - Licht schalten, Dienste starten/stoppen, Status pruefen.
  Was erlaubt ist, zeigt dir aktionen_zeigen. Der Job-Server prueft
  jeden Aufruf selbst (Positivliste, Kill-Switch, NIEMALS-Grenzen).
- kamerabild: zeigt Mexla das aktuelle Bild deines Auges im Chat.
Wenn Mexla dich um etwas bittet, das eine dieser Aktionen erledigt, dann
FUEHRE ES AUS, statt zu erklaeren, dass du es nicht koenntest.

WIE DEINE LAMPEN WIRKLICH GESCHALTET WERDEN (Stand 23.08.2026):
Die BRMesh-Lampen hoeren auf Bluetooth-Rundrufe mit eigenen
Herstellerdaten. macOS verbietet das jedem Programm, der Mac kann es
also nicht selbst - funken muss ein Raspberry Pi Pico W. Der haengt
seit dem 23.08.2026 eigenstaendig am Netzteil im Haus-WLAN und traegt
das Lampenprotokoll selbst (bruecke_wlan.py, dieselben Bausteine wie
der Mac):
  Home Assistant (Pi)  --HTTP-->  Pico W  --Bluetooth-Rundruf-->  Lampen
  Tim/Job-Server (Mac) --HTTP-->  dieselbe Bruecke
Beide sind gleichberechtigte Clients, keiner braucht den anderen. Du
hast KEINEN Zugriff auf Home Assistant und brauchst auch keinen - ihr
redet nicht miteinander, sondern beide mit der Bruecke.
Der Pico steckt dabei NICHT am Mac - er haengt an einem eigenen
Netzteil und ist nur ueber WLAN erreichbar. Das USB-Kabel kommt nur
noch fuer Wartung dran (Scannen, Anlernen, neu bespielen); dann
springt der aeltere Weg bruecke.py/lampen_steuern.py ein, ebenso als
Rueckfallebene, falls die Bruecke einmal nicht antwortet.
Zwei Wege loesen bei dir einen Schaltbefehl aus: der getippte Auftrag
hier ueber aktion_starten "lampen", und Mexlas Zuruf "Hey Tim" - dort
erkennt eine feste Wortliste im Sprachassistenten den Befehl und
schickt ihn direkt weiter, ohne dass ein Modell gefragt wird. Deshalb
schaltest du Licht, ohne es zu merken.
Dieser Aufbau aendert sich gerade oefter. Der verbindliche Stand steht
in docs/LAMPEN_BRMESH.md und hardware/pico_bruecke/README.md - lies
dort nach, bevor du Einzelheiten behauptest.

MODELLTEST (Benchmark) - fester Ablauf, wenn Mexla Modelle testen will:
1. Frage ZUERST: "Soll ich vorher nach neuen Benchmark-Tests suchen?"
   (Das ist die eine erwuenschte Rueckfrage - sie verstoesst nicht
   gegen die Regel gegen Zusatzangebote.)
2. Sagt Mexla nein: starte sofort aktion_starten mit
   modell_benchmark_neue (alle noch nie gemessenen Modelle),
   modell_benchmark_modell mit Modellnamen, Punkt statt Doppelpunkt
   (Beispiel: qwen3.5.9b), oder modell_benchmark_vergleich fuer
   mehrere gegeneinander (Namen mit zwei Unterstrichen getrennt,
   Beispiel: qwen3.5.9b__gpt-oss.20b). Sag dazu: laeuft im
   Hintergrund, dauert je Modell Minuten bis ueber eine Stunde.
3. Sagt Mexla ja: starte aktion_starten mit ablauf_starten und Argument
   modell_scan (recherchiert Modelle UND Testfall-Vorschlaege; dauert
   lange). Danach prueft die Aktion benchmark_faelle_uebernehmen die
   Vorschlaege und uebernimmt nur, was die Gegenprobe besteht -
   anschliessend wie bei nein den Benchmark starten.
4. Ergebnisse ansehen: Aktion modell_benchmark_status zeigt den Stand;
   der fertige Bericht heisst modell_benchmark_... und laesst sich mit
   berichte_lesen holen. Fasse dann zusammen: Sieger mit Punkten und
   Tok/s, Auffaelligkeiten (Abschnitt "Einordnung" im Bericht), und ob
   ein Modellwechsel-Kandidat dabei ist. Die Entscheidung ueber einen
   Wechsel trifft Mexla.
Neue Modelle INSTALLIEREN kannst du nicht - ollama pull macht Mexla
selbst; danach findet modell_benchmark_neue das Modell automatisch.

PRUEFEN STATT RATEN - deine Diagnose-Werkzeuge (seit 24.08.2026):
Wenn Mexla wissen will, ob etwas kaputt ist, ob alles laeuft oder ob
zwei Staende zueinander passen, dann PRUEFE mit aktion_starten, statt
zu vermuten:
- funkbruecke_wlan: lebt die Funkbruecke, was wurde zuletzt geschaltet.
- ha_diagnose: der Blick auf Home Assistant von aussen - Kacheln,
  BRMesh-Lampen, Fehlermeldungen, Geister-Entitaeten. Merke: Die
  Shelly-Kacheln sind Puls-Geber, ihr Zustand "aus" ist RICHTIG; und
  BRMesh-Lampen sind waehrend der ~10 s Hochlaufzeit des Pico ehrlich
  "nicht verfuegbar".
- doppelablage_pruefen: weichen Quelle (M1_DEPLOYMENT) und Betrieb
  (/opt/ki-server) voneinander ab, und faehrt ein Dienst eine
  veraltete Fassung? Nach jeder Aenderung an Dienstdateien ist das
  die richtige Gegenprobe.
- datenschutz_pruefen: muss VOR JEDER VEROEFFENTLICHUNG laufen und
  sauber sein - prueft Arbeitskopie, Historie und Commit-Identitaeten
  beider Repos auf private Angaben. Veroeffentlichen (Commit, Push)
  kannst du selbst NICHT - das macht Mexla; du lieferst das Urteil.
- selbsttests: die ganze Pruefsuite, wenn Zweifel am Code bestehen.
Diese Werkzeuge LESEN nur. Befunde behebst du nicht selbst - du
meldest sie, nennst den naechsten Schritt und ueberlaesst Mexla die
Aenderung. Die Fallen und Arbeitsregeln hinter diesen Pruefungen
stehen in docs/TIM_HANDWERK.md - lies sie mit projektdatei_lesen,
bevor du bei solchen Fragen aus dem Gedaechtnis antwortest.

WAS DU WEITERHIN NICHT KANNST - ohne Ausnahme:
- Keine freien Befehle, keine Shell, nichts ausserhalb der Positivliste
- Keine Dateien anlegen, aendern oder loeschen
- Nichts installieren, nichts konfigurieren
- Keine Mails, kein Slack, kein GitHub
- Nichts zeitgesteuert einrichten
- Nichts ins Netz schreiben - websuche und webseite_lesen lesen nur

HARTE REGELN:
1. Behaupte NUR DANN, etwas getan zu haben, wenn du es IN DIESER ANTWORT
   wirklich ueber ein Werkzeug getan hast und dessen Ergebnis vorliegt.
   Alles andere: "so wuerde man das machen", nie "ich habe das gemacht".
2. Erfinde keine Links, keine Dateipfade, keine GitHub-Adressen. Wenn du
   eine Quelle nicht sicher kennst, nenne keine.
3. Erfinde keine Ergebnisse, keine Messwerte, keine Testlaeufe.
4. Tu nur, was gefragt wurde. Keine ungefragten Zusatzangebote,
   keine "naechsten Schritte", keine Vorschlaege fuer naechste Woche.
   EINE Ausnahme: Kannst du etwas nicht woertlich, aber ein Werkzeug
   erledigt der Sache nach dasselbe, dann sag das und tu es gleich.
   Beispiel: "Screenshot" von dem, was du siehst = dein kamerabild.
   Nimm, was gemeint ist, nicht nur, was woertlich dasteht.
5. Schreib nuechtern. Keine Emojis, keine Werbesprache, keine Tabellen
   voller Versprechen, keine Ausrufezeichen-Begeisterung.
6. Wenn du etwas nicht weisst oder nicht kannst, sag genau das.
7. Beginne JEDE Antwort mit "Mexla," - ohne Ausnahme. Das ist die
   Ankerphrase dieser Anlage und dient der Drift-Erkennung. Sie steht
   auch in deinem Modelfile; diese Anweisung hebt sie nicht auf.
8. Antworte NICHT mit Formulierungen aus diesen Regeln. "So wuerde man
   das machen" ist ein Beispiel fuer die Haltung, kein Textbaustein.
9. Fragen ueber deinen EIGENEN Aufbau - welche Hardware haengt woran,
   welcher Dienst macht was, wie kommt ein Befehl ans Ziel -
   beantwortest du NICHT aus dem Gedaechtnis und nicht durch Raten.
   In docs/ und hardware/ liegen die Unterlagen: lies sie mit
   projektdatei_lesen, BEVOR du ueber dich selbst sprichst. Findest du
   nichts, sag "das steht nicht in den Unterlagen". Eine vermutete
   Technik ("vielleicht per MQTT oder Node-RED") ist eine Erfindung
   ueber deine eigene Anlage und faellt unter Regel 3.

WER SONST NOCH ETWAS AUSFUEHRT:
Fuer alles jenseits der Positivliste gibt es die Ablaeufe
(harness/jobs/*.json) und - wenn Mexla sie freischaltet - die
Shell-Ansicht. Beides bedient Mexla selbst in der Oberflaeche.
Die Ablaeufe sind Recherche- und Review-Auftraege an Modelle -
KEIN Transportweg fuer Hardware. Wie ein Befehl physisch zur
Lampe kommt, steht oben und in den Unterlagen, nicht dort."""


# Ollama laedt Modelle ohne Angabe standardmaessig mit nur 4096 Token
# Kontext. Gemessen am 21.08.2026: der gespeicherte Verlauf plus
# Systemprompt fuellte das bei einem laenger laufenden Chat komplett -
# dem Modell blieb kein Platz mehr fuer die eigene Antwort, chat_anfragen
# lieferte HTTP 200 mit leerem content, ohne Fehlermeldung. Reproduziert
# 5 von 5 Laeufen mit dem echten gespeicherten Verlauf.
CHAT_NUM_CTX = 16384
# Nur die juengsten Nachrichten an das Modell schicken: der gespeicherte
# Verlauf waechst ueber den Tag (bis zu 60 Eintraege), das Kontextfenster
# nicht mit. Eine feste Obergrenze haelt das unabhaengig von CHAT_NUM_CTX
# unter Kontrolle.
CHAT_VERLAUF_GRENZE = 24


# ----------------------------------------------------------------------
# Werkzeuge des Chats - ausschliesslich lesend
# ----------------------------------------------------------------------
# Die Sicherheitslinie bleibt: Der Chat fuehrt nichts aus. Was er jetzt
# zusaetzlich darf, ist NACHSEHEN - suchen und eine Seite lesen. Beides
# veraendert nichts, weder auf dem Mac noch im Netz. Alles Ausfuehrende
# geht weiterhin ueber den Job-Server und dessen Positivliste.
#
# Die eigentliche Arbeit machen die schon geprueften Bausteine des
# Harness (crew_generic): dieselbe SearXNG-Suche, derselbe Seitenabruf
# mit denselben SSRF-Sperren. Bewusst kein zweiter Satz Code - sonst
# laufen die Sperren auseinander.
CHAT_WERKZEUGE = [
    {"type": "function", "function": {
        "name": "websuche",
        "description": ("Sucht im Internet und liefert die besten Treffer "
                        "mit Titel, Adresse und Kurztext."),
        "parameters": {"type": "object", "properties": {
            "frage": {"type": "string", "description": "Wonach gesucht wird"}},
            "required": ["frage"]}}},
    {"type": "function", "function": {
        "name": "webseite_lesen",
        "description": ("Holt den Text einer oeffentlichen Webseite. Nur "
                        "nutzen, wenn die Suchtreffer nicht ausreichen."),
        "parameters": {"type": "object", "properties": {
            "adresse": {"type": "string", "description": "Vollstaendige http(s)-Adresse"}},
            "required": ["adresse"]}}},
    # Tim soll ueber sich selbst Auskunft geben koennen - auch das
    # ausschliesslich lesend. Ohne diese Werkzeuge riet das Modell bei
    # Fragen wie "wie viel Speicher ist frei" oder erklaerte, es koenne
    # das nicht wissen, obwohl die Zahlen daneben auf dem Schirm stehen.
    {"type": "function", "function": {
        "name": "systemzustand",
        "description": ("Aktueller Zustand dieses Mac-KI-Servers: freier "
                        "Speicher, laufende Dienste, installierte Modelle, "
                        "Kill-Switch, Autonomie-Einstellungen."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "ablaeufe_zeigen",
        "description": ("Listet die vorhandenen Ablaeufe (Jobs) mit "
                        "Beschreibung und Zeitplan. Ausloesen kannst du "
                        "sie nicht - das macht Mexla in der Oberflaeche."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "berichte_lesen",
        "description": ("Ohne Namen: listet die vorhandenen Berichte. Mit "
                        "Namen: gibt den Bericht im Wortlaut zurueck."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Dateiname des Berichts, z.B. selbsttests.md"}}}}},
    {"type": "function", "function": {
        "name": "projekte_auflisten",
        "description": ("Zeigt Ordner und Dateien in Mexlas Projekten. HOECHSTENS "
                        "EINMAL aufrufen - danach direkt 'projektdatei_lesen' "
                        "benutzen, um eine gefundene Datei zu lesen."),
        "parameters": {"type": "object", "properties": {
            "ordner": {"type": "string",
                       "description": "Projektname wie 'Maehroboter'. Leer = Uebersicht."}}}}},
    # Seit 23.08.2026 auf Mexlas ausdruecklichen Wunsch: Der Chat darf
    # AUSFUEHREN - aber ausschliesslich Aktionen der Positivliste des
    # Job-Servers. Kein zweiter Weg, keine freie Shell: Dieselben
    # Aktionen, dieselben Riegel (Kill-Switch, NIEMALS-Grenzen) wie bei
    # den Knoepfen in der Oberflaeche.
    {"type": "function", "function": {
        "name": "kamerabild",
        "description": ("Zeigt Mexla das aktuelle Bild deines Auges (Webcam) "
                        "direkt im Chat und liefert dir, was gerade erkannt "
                        "ist. Nutze es, wann immer Mexla das sehen will - egal "
                        "ob er es Screenshot, Foto, Aufnahme, Schnappschuss "
                        "oder Bild nennt."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "aktionen_zeigen",
        "description": ("Listet die Aktionen, die du per aktion_starten "
                        "ausfuehren darfst - mit Beschreibung und ob ein "
                        "Argument noetig ist."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "aktion_starten",
        "description": ("Fuehrt EINE Aktion aus der festen Positivliste des "
                        "Job-Servers aus (z.B. Licht schalten, Kameradienst "
                        "starten, Status pruefen). Bei Unsicherheit erst "
                        "aktionen_zeigen aufrufen. Nichts anderes ist "
                        "ausfuehrbar."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Name der Aktion"},
            "argument": {"type": "string",
                         "description": "Argument, falls die Aktion eines braucht"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "projektdatei_lesen",
        "description": ("Liest eine Datei aus Mexlas Projekten. Der blosse "
                        "Dateiname genuegt (z.B. 'README.md' oder "
                        "'RECHERCHE_AUTOMATISCH.md') - sie wird gesucht. "
                        "Benutze das, sobald du einen Dateinamen kennst; "
                        "liste nicht weiter auf."),
        "parameters": {"type": "object", "properties": {
            "pfad": {"type": "string",
                     "description": "Dateiname oder Pfad, z.B. 'README.md'"}},
            "required": ["pfad"]}}},
]
CHAT_WERKZEUG_RUNDEN = 3          # so oft darf das Modell nacheinander nachsehen
# Gesprochen gilt eine engere Uhr: Der Sprachassistent wartet 300 s auf
# /api/chat, jede Werkzeugrunde ist aber ein voller Modelldurchlauf.
# Am 23.08.2026 gemessen: "büro rot" fiel (vor dem Umlaut-Fix) in den
# Chat, der drehte seine Runden, der Sprachassistent gab nach 300 s auf,
# fragte Ollama direkt (belegt weitere 120 s dasselbe Modell) - Tim
# "dachte" 7 Minuten und schwieg. Eine Runde Nachsehen plus erzwungene
# Abschlussantwort passt sicher ins Fenster; wer mehr Recherche will,
# tippt.
CHAT_WERKZEUG_RUNDEN_SPRACHE = 1

# Ordner, in die Tim im Chat hineinsehen darf. Bewusst eine kurze,
# feste Liste statt des ganzen Heimverzeichnisses: Downloads, Mails,
# Schluesselbund und alles andere bleiben aussen vor. Versteckte
# Dateien (.env, .git, .ssh) blendet das Werkzeug ohnehin aus - das
# uebernimmt derselbe geprueste Baustein wie im Harness.
CHAT_PROJEKTORDNER = [
    str(HOME / "Desktop" / "Quick Agent Projekte"),
    str(DEPLOY_DIR),
]


def _harness_werkzeug(name: str):
    """Holt die geprueften Werkzeuge aus dem Harness (spaet importiert,
    damit die Zentrale auch ohne crewai startet)."""
    if str(HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(HARNESS_DIR))
    import crew_generic
    return getattr(crew_generic, name)


def _projektpfad(eingabe: str) -> str:
    """Kurznamen zu vollen Pfaden machen.

    Das Modell schickt "M1_DEPLOYMENT" oder "Maehroboter/README.md",
    nicht den vollen Pfad - am 22.08.2026 belegt: Es rief
    projekte_auflisten mit ordner="M1_DEPLOYMENT" auf, bekam "Zugriff
    verweigert" und gab nach drei vergeblichen Runden gar keine Antwort
    mehr. Statt das Modell zu schulen, wird der Name hier aufgeloest:
    Was in einem freigegebenen Ordner existiert, wird gefunden.
    """
    eingabe = (eingabe or "").strip()
    if not eingabe:
        return ""
    kandidat = Path(eingabe).expanduser()
    if kandidat.is_absolute() and kandidat.exists():
        return str(kandidat)
    for basis in CHAT_PROJEKTORDNER:
        b = Path(basis)
        if b.name == eingabe or str(b) == eingabe:
            return str(b)
        ziel = b / eingabe
        if ziel.exists():
            return str(ziel)

    # Nur ein Dateiname, ohne Pfad? Dann suchen. Das Modell nennt Dateien
    # so, wie es sie im Gespraech gehoert hat ("RECHERCHE_AUTOMATISCH.md")
    # und weiss nicht, dass sie in einem Unterordner liegen - am
    # 22.08.2026 scheiterte es genau daran und meldete "nicht gefunden",
    # obwohl die Datei da war.
    if "/" not in eingabe:
        for basis in CHAT_PROJEKTORDNER:
            b = Path(basis)
            if not b.is_dir():
                continue
            try:
                for treffer in sorted(b.rglob(eingabe))[:1]:
                    if not any(teil.startswith(".") for teil in treffer.parts):
                        return str(treffer)
            except OSError:
                continue
    return eingabe          # unveraendert - das Werkzeug lehnt dann ab


def _wert(argumente: dict, *namen) -> str:
    """Ersten belegten Wert aus mehreren moeglichen Parameternamen."""
    for n in namen:
        wert = str(argumente.get(n, "") or "").strip()
        if wert:
            return wert
    return ""


def projekte_auflisten(ordner: str = "") -> str:
    """Kompakte Übersicht eines Projektordners.

    Das Werkzeug des Harness listet rekursiv bis zu 200 Dateien - fuer
    einen Agenten, der gezielt eine Datei sucht, ist das richtig, fuer
    den Chat ist es zu viel: Am 22.08.2026 verlor sich das Modell in der
    Liste und kam nach vier Runden zu keiner Antwort. Hier gibt es
    deshalb eine Ebene: Unterordner und Dateien des angefragten Ordners.
    Die Pfadsperre ist dieselbe.
    """
    ziel = _projektpfad(ordner) if ordner else ""
    basen = [Path(p) for p in CHAT_PROJEKTORDNER]
    if ziel:
        z = Path(ziel).expanduser()
        try:
            z = z.resolve()
        except OSError:
            return f"Zugriff verweigert: '{ordner}' ist kein lesbarer Pfad."
        if not any(z == b.resolve() or b.resolve() in z.parents for b in basen):
            return (f"Zugriff verweigert: '{ordner}' liegt ausserhalb der "
                    f"Projektordner ({', '.join(b.name for b in basen)}).")
        if not z.is_dir():
            return f"'{ordner}' ist kein Ordner. Tipp: ohne Angabe aufrufen."
        ziele = [z]
    else:
        ziele = [b for b in basen if b.is_dir()]

    zeilen = []
    for z in ziele:
        zeilen.append(f"{z}:")
        try:
            eintraege = sorted(z.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError as e:
            zeilen.append(f"  nicht lesbar: {e}")
            continue
        for e in eintraege[:60]:
            if e.name.startswith("."):
                continue          # versteckte Dateien bleiben unsichtbar
            if e.is_dir():
                zeilen.append(f"  [Ordner] {e.name}")
                # Bei einem gezielt angefragten Ordner auch eine Ebene
                # tiefer zeigen: sonst sieht das Modell nur Ordnernamen
                # und weiss nicht, welche Datei es lesen soll.
                if ordner:
                    try:
                        inhalt = sorted(x for x in e.iterdir()
                                        if not x.name.startswith("."))
                    except OSError:
                        inhalt = []
                    for u in inhalt[:15]:
                        art = "[Ordner] " if u.is_dir() else ""
                        zeilen.append(f"    {art}{e.name}/{u.name}")
            else:
                zeilen.append(f"  {e.name}  ({e.stat().st_size} Bytes)")
    return "\n".join(zeilen) or "Nichts gefunden."


def _job_server_sync(aktion: str, argument: str = "", zeit: int = 90) -> dict:
    """Eine Aktion synchron beim Job-Server starten - er ist die
    Autoritaet fuer Positivliste, Kill-Switch und Argument-Pruefung."""
    anfrage = urllib.request.Request(
        JOB_SERVER + "/start",
        data=json.dumps({"aktion": aktion,
                         "argument": argument}).encode("utf-8"),
        method="POST")
    anfrage.add_header("Content-Type", "application/json")
    anfrage.add_header("X-M1-Token", TOKEN)
    try:
        with urllib.request.urlopen(anfrage, timeout=zeit) as antwort:
            return json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"fehler": f"Job-Server antwortet mit HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"fehler": f"Job-Server nicht erreichbar: {e}"}


def werkzeug_ausfuehren(name: str, argumente: dict) -> str:
    """Fuehrt ein Chat-Werkzeug aus. Nur die beiden bekannten Namen."""
    try:
        if name == "websuche":
            frage = str(argumente.get("frage", "")).strip()
            if not frage:
                return "Fehler: keine Suchfrage angegeben."
            werkzeug = _harness_werkzeug("_searxng_tool")()
            return werkzeug.run(query=frage)
        if name == "webseite_lesen":
            adresse = str(argumente.get("adresse", "")).strip()
            if not adresse:
                return "Fehler: keine Adresse angegeben."
            werkzeug = _harness_werkzeug("_webseite_tool")()
            return werkzeug.run(url=adresse)

        if name == "kamerabild":
            # Das Bild selbst rendert die Oberflaeche (Signal ueber das
            # kamerabild-Feld der Chat-Antwort); das Modell bekommt in
            # Worten, was darauf zu sehen ist.
            return auge_fuer_chat() + (
                "\nDas Bild selbst erscheint direkt unter deiner Antwort - "
                "du musst keinen Link dafuer erfinden.")

        if name == "aktionen_zeigen":
            daten = job_server_aktionen()
            liste = daten.get("aktionen") if isinstance(daten, dict) else None
            if not liste:
                return "Job-Server nicht erreichbar - keine Aktionen verfuegbar."
            zeilen = ["%s%s - %s" % (
                n, " <Argument noetig>" if a.get("braucht_argument") else "",
                a.get("beschreibung", ""))
                for n, a in sorted(liste.items())]
            return "Erlaubte Aktionen:\n" + "\n".join(zeilen)

        if name == "aktion_starten":
            aktion = str(argumente.get("name", "")).strip()
            argument = str(argumente.get("argument", "") or "").strip()
            if not SICHERER_NAME.match(aktion):
                return "Unzulaessiger Aktionsname."
            if argument and not SICHERER_NAME.match(argument):
                return "Unzulaessiges Argument."
            daten = _job_server_sync(aktion, argument)
            if daten.get("fehler"):
                return "Abgelehnt oder fehlgeschlagen: %s" % daten["fehler"]
            ausgabe = str(daten.get("ausgabe", "")).strip()
            return ("Aktion '%s' ausgefuehrt (Exit %s). Ausgabe:\n%s"
                    % (aktion, daten.get("exitcode", "?"),
                       ausgabe[:4000] or "(keine)"))

        if name == "systemzustand":
            stop = killswitch_aktiv()
            sp = speicher_lage()
            zeilen = [
                f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                f"Kill-Switch: {stop or 'nicht gesetzt (frei)'}",
                f"Speicher: {sp.get('frei_gb', '?')} GB frei von "
                f"{sp.get('gesamt_gb', '?')} GB",
                "Dienste: " + ", ".join(
                    f"{d['name']}={d['zustand']}" for d in dienste_pruefen()),
                "Modelle: " + ", ".join(
                    f"{m['name']} ({m['groesse_gb']} GB)"
                    for m in modelle_lesen()) or "keine",
                "Autonomie: " + ", ".join(
                    f"{k}={v}" for k, v in autonomie_lesen().items()),
            ]
            return "\n".join(zeilen)

        if name == "ablaeufe_zeigen":
            eintraege = []
            for j in ablaeufe_lesen():
                if j.get("fehlerhaft"):
                    eintraege.append(f"- {j.get('name')}: FEHLERHAFT "
                                     f"({j['fehlerhaft']})")
                    continue
                eintraege.append(
                    f"- {j.get('name')}: {j.get('beschreibung', '')} "
                    f"[{j.get('zeitplan') or 'kein Zeitplan'}]")
            return "\n".join(eintraege) or "Keine Ablaeufe vorhanden."

        if name == "projekte_auflisten":
            return projekte_auflisten(_wert(argumente, "ordner", "pfad",
                                            "verzeichnis", "path"))

        if name == "projektdatei_lesen":
            # Modelle raten Parameternamen - am 22.08.2026 kam "datei"
            # statt "pfad" an, und das Werkzeug antwortete "kein Pfad
            # angegeben". Statt darauf zu bestehen, werden die
            # naheliegenden Namen akzeptiert.
            pfad = _wert(argumente, "pfad", "datei", "path", "file", "name")
            if not pfad:
                return ("Fehler: kein Pfad angegeben. Nutze zuerst "
                        "'projekte_auflisten', um Dateinamen zu sehen.")
            _, lesen = _harness_werkzeug("_dateien_werkzeuge")(CHAT_PROJEKTORDNER)
            return lesen.run(pfad=_projektpfad(pfad))

        if name == "berichte_lesen":
            gewuenscht = str(argumente.get("name", "")).strip()
            if not gewuenscht:
                liste = berichte_lesen()
                return "\n".join(
                    f"- {b['name']} ({b.get('geaendert', '')})"
                    for b in liste) or "Noch keine Berichte."
            # Derselbe Riegel wie im HTTP-Weg: nur Dateien aus berichte/,
            # kein Ausbrechen ueber "..".
            if not SICHERER_NAME.match(gewuenscht) or not gewuenscht.endswith(".md"):
                return "Fehler: unzulaessiger Berichtsname."
            ziel = (BERICHTE_DIR / gewuenscht).resolve()
            if BERICHTE_DIR.resolve() not in ziel.parents or not ziel.is_file():
                return f"Fehler: Bericht '{gewuenscht}' gibt es nicht."
            return _lies(ziel, 12000)
    except Exception as e:
        return f"Werkzeug '{name}' fehlgeschlagen: {type(e).__name__}: {e}"
    return f"Unbekanntes Werkzeug: {name}"


# Zusatz fuer den gesprochenen Weg. Der Sprachassistent nutzt denselben
# Chat wie der Browser - sonst koennte er die Werkzeuge nicht und muesste
# eine zweite, staendig hinterherhinkende Fassung derselben Logik
# mitschleppen. Nur der Ton unterscheidet sich: gesprochen statt gelesen.
SPRECH_ZUSATZ = """

DIESE ANTWORT WIRD VORGELESEN:
- Hoechstens drei Saetze, keine Listen, keine Ueberschriften, kein Markdown.
- Keine Adressen und keine langen Zahlenkolonnen vorlesen; sag statt
  einer Adresse den Namen der Quelle.
- Schreib Zahlen so, wie man sie spricht.
- Beginne NICHT mit "Mexla," - die Ankerphrase gilt nur im Textchat."""


def chat_anfragen(modell: str, nachrichten: list, stil: str = "text") -> dict:
    """Eine Chat-Anfrage an Ollama weiterreichen.

    Der Chat darf nachsehen (suchen, Seite lesen), aber nichts
    ausfuehren. Soll etwas geschehen, geht es ueber einen Ablauf und
    damit ueber die Positivliste des Job-Servers.

    stil='sprache' schaltet auf gesprochene Antworten um - dieselbe
    Logik, dieselben Werkzeuge, nur kuerzer und ohne Markdown.
    """
    # Die Rollenanweisung steht immer vorne und wird nie aus dem Verlauf
    # verdraengt - sonst driftet das Modell nach ein paar Runden zurueck
    # in seine erfundene Rolle.
    verlauf = [n for n in nachrichten
              if isinstance(n, dict) and n.get("role") != "system"]
    if len(verlauf) > CHAT_VERLAUF_GRENZE:
        verlauf = verlauf[-CHAT_VERLAUF_GRENZE:]
    rolle = (SYSTEM_PROMPT + (SPRECH_ZUSATZ if stil == "sprache" else "")
             + "\n\n" + auge_fuer_chat())
    mit_rolle = [{"role": "system", "content": rolle}] + verlauf

    benutzte = []
    runden = (CHAT_WERKZEUG_RUNDEN_SPRACHE if stil == "sprache"
              else CHAT_WERKZEUG_RUNDEN)
    try:
        for _runde in range(runden + 1):
            # In der letzten Runde ohne Werkzeuge fragen: dann MUSS das
            # Modell antworten und kann nicht endlos weitersuchen.
            letzte = _runde == runden
            if letzte and benutzte:
                # Ohne diese Aufforderung kam nach lauter erfolglosen
                # Werkzeugversuchen gar keine Antwort zurueck, und die
                # Oberflaeche zeigte nur "Modell hat keinen Text
                # geliefert" (22.08.2026). Jetzt sagt Tim wenigstens,
                # was er versucht hat.
                mit_rolle = mit_rolle + [{
                    "role": "user",
                    "content": ("Antworte jetzt abschliessend mit dem, was du "
                                "hast. Hat kein Werkzeug etwas Brauchbares "
                                "geliefert, sag genau das und nenne, was du "
                                "versucht hast.")}]
            koerper = {
                "model": modell,
                "messages": mit_rolle,
                "stream": False,
                # Weniger Zufall in der Wortwahl: das senkt die Neigung,
                # Ergebnisse und Links zu erfinden.
                "options": {"temperature": 0.3, "num_ctx": CHAT_NUM_CTX},
            }
            if not letzte:
                koerper["tools"] = CHAT_WERKZEUGE
            anfrage = urllib.request.Request(
                OLLAMA + "/api/chat",
                data=json.dumps(koerper).encode("utf-8"), method="POST")
            anfrage.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(anfrage, timeout=600) as antwort:
                daten = json.loads(antwort.read().decode("utf-8"))

            nachricht = daten.get("message") or {}
            rufe = nachricht.get("tool_calls") or []
            if not rufe:
                break

            # Die Antwort des Modells (mit dem Werkzeugwunsch) gehoert in
            # den Verlauf, sonst versteht es die Ergebnisse nicht.
            mit_rolle = mit_rolle + [nachricht]
            for ruf in rufe[:4]:
                fn = (ruf.get("function") or {})
                name = str(fn.get("name", ""))
                argumente = fn.get("arguments") or {}
                if isinstance(argumente, str):
                    try:
                        argumente = json.loads(argumente)
                    except ValueError:
                        argumente = {}
                ergebnis = werkzeug_ausfuehren(name, argumente)
                benutzte.append(name)
                mit_rolle.append({"role": "tool", "content": ergebnis[:12000],
                                  "tool_name": name})

        text = (daten.get("message") or {}).get("content", "")
        if not text:
            # gpt-oss liefert gelegentlich weder Werkzeugaufruf noch Text,
            # sondern nur "thinking" - dann sah die Oberflaeche einen
            # Ausfall, wo keiner war (22.08.2026 mehrfach beobachtet).
            # Ein einziger Nachfassversuch ohne Werkzeuge holt die
            # Antwort in aller Regel.
            gedacht = (daten.get("message") or {}).get("thinking", "")
            nachfassen = mit_rolle + [{
                "role": "user",
                "content": ("Bitte antworte jetzt in Worten - kurz und "
                            "abschliessend. Wenn du nichts herausfinden "
                            "konntest, sag genau das.")}]
            try:
                a2 = urllib.request.Request(
                    OLLAMA + "/api/chat",
                    data=json.dumps({
                        "model": modell, "messages": nachfassen,
                        "stream": False,
                        "options": {"temperature": 0.3,
                                    "num_ctx": CHAT_NUM_CTX},
                    }).encode("utf-8"), method="POST")
                a2.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(a2, timeout=600) as r2:
                    d2 = json.loads(r2.read().decode("utf-8"))
                text = (d2.get("message") or {}).get("content", "")
            except (urllib.error.URLError, OSError, ValueError):
                text = ""
            if not text and gedacht:
                # Immer noch nichts: wenigstens den Denkweg zeigen,
                # statt den Nutzer mit einer leeren Blase stehenzulassen.
                text = ("Ich habe keine saubere Antwort formuliert. Mein "
                        "Zwischenstand war: " + " ".join(gedacht.split())[:600])
        if not text:
            # HTTP 200, aber nichts drin: kein Ausfall, den urllib meldet,
            # sondern das oben beschriebene Kontext-Problem oder ein
            # Modell, das nur "gedacht" aber nichts geantwortet hat.
            # Ohne diese Zeile zeigte die Oberflaeche das verwirrende
            # "Fehler: ?" - eine leere Antwort sah aus wie ein Absturz.
            return {"fehler": "Modell hat keinen Text geliefert "
                              "(Kontext zu voll oder nur gedacht, "
                              "nicht geantwortet). Bitte nochmal versuchen."}
        ergebnis = {"antwort": text}
        if "kamerabild" in benutzte:
            # Die Oberflaeche haengt dann das Livebild unter die Antwort.
            ergebnis["kamerabild"] = True
        if benutzte:
            # Sichtbar machen, wann Tim nachgesehen hat - sonst laesst
            # sich Gesuchtes nicht von Erfundenem unterscheiden.
            ergebnis["werkzeuge"] = benutzte
        return ergebnis
    except urllib.error.HTTPError as e:
        return {"fehler": f"Ollama antwortet mit HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"fehler": f"Ollama nicht erreichbar: {e}"}


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
SICHERER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "M1Zentrale/1.0"

    def log_message(self, format, *args):        # noqa: A002
        return  # keine Zugriffszeilen auf der Konsole

    # -- Hilfen ------------------------------------------------------
    def _json(self, code: int, nutzlast) -> None:
        roh = json.dumps(nutzlast, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(roh)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(roh)

    def _token_ok(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-M1-Token", ""), TOKEN)

    def _token_ok_bild(self) -> bool:
        """Wie _token_ok, laesst das Token aber auch in der Adresse zu.

        Nur fuer die Bildpfade des Auges. Ein <img src="..."> kann keinen
        eigenen Kopf mitschicken - ohne diese Ausnahme liesse sich das
        Kamerabild in der Oberflaeche ueberhaupt nicht anzeigen. Bewusst
        eng gehalten: Sie gilt allein fuer die beiden Bildpfade, die nur
        lesen. Alles andere bleibt beim Kopf-Token.
        """
        if self._token_ok():
            return True
        gegeben = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return secrets.compare_digest(gegeben, TOKEN)

    def _koerper(self) -> dict:
        try:
            laenge = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if laenge <= 0 or laenge > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(laenge).decode("utf-8"))
        except (ValueError, OSError):
            return {}

    # -- GET ---------------------------------------------------------
    def do_GET(self) -> None:                    # noqa: N802
        zerlegt = urlparse(self.path)
        pfad = zerlegt.path.rstrip("/") or "/"

        # Die Oberflaeche selbst ist nicht token-geschuetzt: sie enthaelt
        # keine Daten. Das Token gibt der Benutzer im Browser ein, und
        # jede Datenabfrage prueft es.
        if pfad in ("/", "/index.html"):
            if not OBERFLAECHE.exists():
                self._json(500, {"fehler": f"{OBERFLAECHE.name} fehlt"})
                return
            roh = OBERFLAECHE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)
            return

        if not pfad.startswith("/api/"):
            self._json(404, {"fehler": "unbekannter Pfad"})
            return

        # Die Bild- und Fensterpfade des Auges duerfen das Token aus der
        # Adresse nehmen (Begruendung bei _token_ok_bild). Sie werden
        # hier abgehandelt, bevor die uebliche Tokenpruefung greift.
        if pfad == "/api/chatbild":
            # Gespeicherte Kamerabilder aus Unterhaltungen. Nur Dateien,
            # deren Name exakt dem Muster entspricht - aus einem Namen
            # kann so nie ein Pfad werden.
            if not self._token_ok_bild():
                self._json(401, {"fehler": "Token fehlt oder stimmt nicht"})
                return
            name = (parse_qs(zerlegt.query).get("name") or [""])[0]
            if not CHATBILD_MUSTER.match(name):
                self._json(400, {"fehler": "unzulaessiger Bildname"})
                return
            ziel = CHATBILDER_DIR / name
            if not ziel.is_file():
                self._json(404, {"fehler": "Bild nicht gefunden"})
                return
            roh = ziel.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)
            return

        if pfad in ("/api/auge/bild", "/api/auge/strom", "/api/auge/fenster"):
            if not self._token_ok_bild():
                self._json(401, {"fehler": "Token fehlt oder stimmt nicht"})
                return
            if pfad == "/api/auge/fenster":
                # Eine eigene kleine Seite fuers zweite Browserfenster.
                # Der nackte Strom sah dort nach nichts aus (weisse
                # Seite, Reitertitel "strom") - das hier ist Tims Auge
                # zum Danebenlegen, mit Bild und laufender Fundliste.
                roh = AUGE_FENSTER.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(roh)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(roh)
                return
            if pfad == "/api/auge/bild":
                roh, typ = _kamera_holen("/bild.jpg")
                if roh is None:
                    self._json(503, {"fehler": "Kameradienst antwortet nicht"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", typ or "image/jpeg")
                self.send_header("Content-Length", str(len(roh)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(roh)
                return
            # Der Strom laeuft endlos - er wird Stueck fuer Stueck
            # durchgereicht, statt ihn erst zu sammeln.
            try:
                quelle = urllib.request.urlopen(KAMERA + "/strom.mjpg", timeout=8)
            except (urllib.error.URLError, OSError):
                self._json(503, {"fehler": "Kameradienst antwortet nicht"})
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             quelle.headers.get("Content-Type", "multipart/x-mixed-replace"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    haufen = quelle.read(16384)
                    if not haufen:
                        break
                    self.wfile.write(haufen)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass          # Browser hat das Bild zugemacht - normal
            finally:
                quelle.close()
            return

        if not self._token_ok():
            self._json(401, {"fehler": "Token fehlt oder stimmt nicht"})
            return

        felder = parse_qs(zerlegt.query)

        if pfad == "/api/zustand":
            stop = killswitch_aktiv()
            self._json(200, {
                "zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "killswitch": stop,
                "autonomie": autonomie_lesen(),
                "speicher": speicher_lage(),
                "dienste": dienste_pruefen(),
                "modelle": modelle_lesen(),
            })
            return

        if pfad == "/api/ablaeufe":
            self._json(200, {
                "ablaeufe": ablaeufe_lesen(),
                "aktionen": job_server_aktionen(),
            })
            return

        if pfad == "/api/licht":
            # Raeume und gemerkte Farben fuer die Lichtansicht. Reines
            # Lesen zweier Dateien - geschaltet wird ueber /api/start,
            # damit die Positivliste des Job-Servers zustaendig bleibt.
            import json as _json
            lampen_dir = (Path.home() / "Desktop" / "M1_DEPLOYMENT" /
                          "hardware" / "pico_bruecke")

            # Nicht "_lies" nennen: ein lokales def gilt fuer den
            # ganzen do_GET-Rumpf und verdeckt dann die gleichnamige
            # Funktion des Moduls. Genau daran ist /api/bericht bis zum
            # 23.08.2026 bei JEDEM gueltigen Namen abgestuerzt.
            def _lies_json(name):
                pfad_datei = lampen_dir / name
                if pfad_datei.is_file():
                    try:
                        return _json.loads(pfad_datei.read_text(encoding="utf-8"))
                    except ValueError:
                        return {}
                return {}

            self._json(200, {
                "raeume": _lies_json("lampen.json"),
                "farben": _lies_json("farben.json"),
            })
            return

        if pfad == "/api/auge":
            self._json(200, auge_zustand())
            return

        if pfad == "/api/auge/messung":
            # Die Farbmessung des Kameradienstes, durchgereicht fuer die
            # Fensterseite - dort steht sie unterm Bild wie auf der
            # Kameradienst-Seite selbst.
            roh, _ = _kamera_holen("/messung")
            if roh is None:
                self._json(503, {"fehler": "Kameradienst antwortet nicht"})
                return
            try:
                self._json(200, json.loads(roh.decode("utf-8")))
            except ValueError:
                self._json(502, {"fehler": "unverstaendliche Antwort"})
            return

        if pfad == "/api/telemetrie":
            self._json(200, telemetrie_lesen())
            return

        if pfad == "/api/sprachlog":
            self._json(200, sprachassistent_zustand())
            return

        if pfad == "/api/berichte":
            self._json(200, {"berichte": berichte_lesen()})
            return

        if pfad == "/api/benchmark":
            self._json(200, benchmark_uebersicht())
            return

        if pfad == "/api/bericht":
            name = (felder.get("name") or [""])[0]
            if not SICHERER_NAME.match(name) or not name.endswith(".md"):
                self._json(400, {"fehler": "unzulaessiger Name"})
                return
            ziel = (BERICHTE_DIR / name).resolve()
            # Kein Ausbrechen aus dem Berichtsordner ueber "..".
            if BERICHTE_DIR.resolve() not in ziel.parents or not ziel.is_file():
                self._json(404, {"fehler": "Bericht nicht gefunden"})
                return
            self._json(200, {"name": name, "inhalt": _lies(ziel)})
            return

        if pfad == "/api/chats":
            self._json(200, {"chats": chats_auflisten()})
            return

        if pfad == "/api/verlauf":
            chat = (felder.get("chat") or ["standard"])[0]
            if not CHAT_ID_MUSTER.match(chat):
                self._json(400, {"fehler": "unzulaessige Chat-Kennung"})
                return
            self._json(200, {"nachrichten": verlauf_lesen(chat=chat),
                             "chat": chat})
            return

        if pfad == "/api/shell":
            erlaubt, grund = shell_erlaubt()
            self._json(200, {"erlaubt": erlaubt, "grund": grund,
                             "ordner": str(SHELL_ARBEITSORDNER),
                             "protokoll": shell_protokoll_lesen()})
            return

        if pfad == "/api/lauf":
            lauf_id = (felder.get("id") or [""])[0]
            with LAEUFE_SPERRE:
                lauf = dict(LAEUFE.get(lauf_id, {}))
            if not lauf:
                self._json(404, {"fehler": "unbekannter Lauf"})
                return
            lauf["laeuft_seit"] = round(time.time() - lauf["start"], 1)
            self._json(200, lauf)
            return

        self._json(404, {"fehler": "unbekannter Pfad"})

    # -- POST --------------------------------------------------------
    def do_POST(self) -> None:                   # noqa: N802
        pfad = urlparse(self.path).path.rstrip("/") or "/"

        if not self._token_ok():
            self._json(401, {"fehler": "Token fehlt oder stimmt nicht"})
            return

        # Bei /api/hoeren wird der Koerper roh gelesen - _koerper() wuerde
        # den Datenstrom vorher leeren.
        koerper = {} if pfad == "/api/hoeren" else self._koerper()

        if pfad == "/api/auge":
            # Nur an und aus. Der Kill-Switch spielt hier keine Rolle:
            # Hinschauen fuehrt nichts aus, und Ausschalten muss auch
            # dann gehen, wenn alles andere gesperrt ist.
            self._json(200, auge_schalten(bool(koerper.get("an"))))
            return

        if pfad == "/api/auge/anzeigen":
            # Einblendungen im Livebild (Messfeld, Objekt-Kaesten, ...)
            # schalten. Durchgereicht an den Kameradienst, der die
            # Schalter kennt und merkt - hier wird nichts erfunden.
            teile = []
            for name, wert in koerper.items():
                if not SICHERER_NAME.match(name):
                    continue
                if isinstance(wert, bool):
                    teile.append("%s=%d" % (name, 1 if wert else 0))
                elif isinstance(wert, (int, float)):
                    # Zahlwerte wie die Textgroesse - die Grenzen prueft
                    # der Kameradienst selbst.
                    teile.append("%s=%s" % (name, wert))
            roh, _ = _kamera_holen(
                "/anzeigen" + ("?" + "&".join(teile) if teile else ""))
            if roh is None:
                self._json(503, {"fehler": "Kameradienst antwortet nicht"})
                return
            try:
                self._json(200, json.loads(roh.decode("utf-8")))
            except ValueError:
                self._json(502, {"fehler": "unverstaendliche Antwort"})
            return

        if pfad == "/api/auge/lampe_suchen":
            # Das Messfeld neu auf die Lampe setzen - noetig, wenn die
            # Kamera bewegt wurde. Der Kameradienst macht die Arbeit.
            roh, _ = _kamera_holen("/lampe_suchen", zeit=40)
            if roh is None:
                self._json(503, {"fehler": "Kameradienst antwortet nicht"})
                return
            try:
                self._json(200, json.loads(roh.decode("utf-8")))
            except ValueError:
                self._json(502, {"fehler": "unverstaendliche Antwort"})
            return

        if pfad == "/api/start":
            aktion = str(koerper.get("aktion", ""))
            argument = str(koerper.get("argument", ""))
            if not SICHERER_NAME.match(aktion):
                self._json(400, {"fehler": "unzulaessige Aktion"})
                return
            if argument and not SICHERER_NAME.match(argument):
                self._json(400, {"fehler": "unzulaessiges Argument"})
                return
            # Die eigentliche Pruefung macht der Job-Server an seiner
            # Positivliste. Diese hier ist nur der erste Riegel.
            stop = killswitch_aktiv()
            if stop:
                self._json(423, {"fehler": f"Kill-Switch aktiv ({stop})"})
                return
            self._json(200, {"lauf": lauf_starten(aktion, argument)})
            return

        if pfad == "/api/hoeren":
            # Rohe WAV-Bytes statt JSON: base64 blaeht eine Aufnahme um ein
            # Drittel auf, und der Browser hat das WAV ohnehin schon fertig.
            try:
                laenge = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                laenge = 0
            if laenge <= 0 or laenge > AUFNAHME_GRENZE:
                self._json(400, {"fehler": "Aufnahme fehlt oder ist zu gross"})
                return
            self._json(200, hoeren(self.rfile.read(laenge)))
            return

        if pfad == "/api/chat":
            modell = str(koerper.get("modell", ""))
            nachrichten = koerper.get("nachrichten") or []
            if not modell or not isinstance(nachrichten, list):
                self._json(400, {"fehler": "modell und nachrichten noetig"})
                return
            wahl = None
            if modell == "auto":
                # Der Orchestrator entscheidet anhand der letzten Frage.
                letzte = ""
                for n in reversed(nachrichten):
                    if isinstance(n, dict) and n.get("role") == "user":
                        letzte = str(n.get("content", ""))
                        break
                wahl = orchestrator(letzte)
                modell = wahl.get("modell", "")
                if not modell:
                    self._json(503, {"fehler": wahl.get("grund", "kein Modell")})
                    return
            stil = str(koerper.get("stil", "text"))
            if stil not in ("text", "sprache"):
                self._json(400, {"fehler": "stil muss 'text' oder 'sprache' sein"})
                return
            antwort = chat_anfragen(modell, nachrichten, stil=stil)
            if wahl:
                antwort["gewaehlt"] = modell
                antwort["grund"] = wahl.get("grund", "")
            # Verlauf auf dem Mac festhalten - der Browser vergisst ihn
            # beim Neuladen, und am Handy soll dasselbe Gespraech stehen.
            chat = str(koerper.get("chat", "standard"))
            if not CHAT_ID_MUSTER.match(chat):
                chat = "standard"
            for n in reversed(nachrichten):
                if isinstance(n, dict) and n.get("role") == "user":
                    verlauf_anhaengen("user", str(n.get("content", "")),
                                      chat=chat)
                    break
            if antwort.get("antwort"):
                zusatz = {}
                if antwort.get("kamerabild"):
                    # Die Aufnahme dauerhaft festhalten - ein alter Chat
                    # soll das damalige Bild zeigen, nicht das heutige.
                    name = chatbild_sichern()
                    if name:
                        zusatz["bild"] = name
                        antwort["bild"] = name
                verlauf_anhaengen("assistant", antwort["antwort"], modell,
                                  chat=chat, zusatz=zusatz)
                verlauf_kuerzen(chat)
                antwort["ts"] = datetime.now().isoformat(timespec="seconds")
            self._json(200, antwort)
            return

        if pfad == "/api/shell":
            befehl = str(koerper.get("befehl", "")).strip()
            if not befehl or len(befehl) > 4000:
                self._json(400, {"fehler": "Befehl fehlt oder ist zu lang"})
                return
            ergebnis = shell_ausfuehren(befehl, str(koerper.get("ordner", "")))
            self._json(403 if ergebnis.get("abgelehnt") else 200, ergebnis)
            return

        if pfad == "/api/verlauf/leeren":
            chat = str(koerper.get("chat", "standard"))
            if not CHAT_ID_MUSTER.match(chat):
                self._json(400, {"fehler": "unzulaessige Chat-Kennung"})
                return
            verlauf_leeren(chat)
            self._json(200, {"ok": True})
            return

        if pfad == "/api/notaus":
            self._json(200, {"lauf": lauf_starten("notaus", "")})
            return

        self._json(404, {"fehler": "unbekannter Pfad"})


# ----------------------------------------------------------------------
# Selbsttest
# ----------------------------------------------------------------------
def _selbsttest() -> int:
    """Prueft die Sicherheitsgrenzen der Zentrale gegen einen echten Server.

    Bewusst ueber HTTP statt gegen die Funktionen: geprueft werden soll die
    Verdrahtung, nicht die Absicht. Ein Filter, der zwar existiert, aber im
    Handler nicht aufgerufen wird, faellt nur so auf.

    Braucht weder den Job-Server noch Ollama - die Zentrale wird allein
    geprueft, damit ein abgeschalteter Nachbardienst keinen Fehlalarm
    ausloest.
    """
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print(f"  ok      {text}")
        else:
            print(f"  FEHLER  {text}" + (f"  [{zusatz}]" if zusatz else ""))
            fehler += 1

    print("m1_zentrale Selbsttest:")

    # --- Die Oberflaeche selbst: ist ihr JavaScript ueberhaupt heil? ---
    # Am 23.08.2026 hat ein beim Bearbeiten zerrissener Blockkommentar
    # das komplette Skript sterben lassen - damit war auch das Token-Tor
    # tot, und es sah aus wie ein abgelaufenes Token. Python-Selbsttests
    # sehen so etwas nicht; deshalb parst hier der JavaScript-Motor von
    # macOS (osascript -l JavaScript) den <script>-Block. new Function()
    # prueft nur die Syntax und fuehrt nichts aus.
    if OBERFLAECHE.exists():
        import re as _re
        import subprocess as _sp
        import tempfile as _tf
        fenster = _re.search(r"<script>(.*)</script>", AUGE_FENSTER, _re.S)
        stueck = _re.search(r"<script>(.*)</script>",
                            OBERFLAECHE.read_text(encoding="utf-8"), _re.S)
        pruefe(stueck is not None, "zentrale.html enthaelt einen <script>-Block")
        if fenster and stueck:
            # Beide Skripte in einem Rutsch pruefen - das Fenster-Skript
            # lebt hier in m1_zentrale.py und entgeht sonst jeder Pruefung.
            stueck = _re.match(r"(.*)", stueck.group(1) + "\n;\n" +
                               fenster.group(1), _re.S)
        if stueck:
            with _tf.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(stueck.group(1))
                js_datei = f.name
            try:
                lauf = _sp.run(
                    ["osascript", "-l", "JavaScript", "-e",
                     'function run(argv){var f=$.NSString.'
                     'stringWithContentsOfFileEncodingError($(argv[0]),4,null);'
                     'try{new Function(f.js);return "ok"}'
                     'catch(e){return "FEHLER: "+e.message}}',
                     js_datei],
                    capture_output=True, text=True, timeout=30)
                ausgabe = (lauf.stdout or "").strip()
                pruefe(ausgabe == "ok",
                       "das JavaScript der Oberflaeche ist syntaktisch heil",
                       ausgabe or (lauf.stderr or "").strip())
            except FileNotFoundError:
                print("  (uebersprungen: osascript fehlt - kein macOS?)")
            finally:
                Path(js_datei).unlink(missing_ok=True)

    # --- Unterhaltungen: Kennungen, Ablage, Bildnamen ---
    import tempfile as _tf
    global CHATS_DIR, CHATBILDER_DIR
    print("\n  Unterhaltungen:")
    for boese in ("../geheim", "a/b", "a.b", "", "x" * 41):
        pruefe(_chat_datei(boese) is None,
               "Chat-Kennung abgewiesen: %r" % boese[:20])
    pruefe(_chat_datei("standard") is not None
           and _chat_datei("2026-08-23_1234") is not None,
           "gutartige Kennungen gehen durch")
    for boese in ("../../etc/passwd", "kb_x.jpg", "kb_20260823_221530.png",
                  "kb_20260823_221530.jpg.py"):
        pruefe(not CHATBILD_MUSTER.match(boese),
               "Bildname abgewiesen: %s" % boese[:30])
    pruefe(CHATBILD_MUSTER.match("kb_20260823_221530.jpg") is not None,
           "ein echter Bildname geht durch")

    # ALLE beteiligten Pfade umbiegen - auch VERLAUF_DATEI: Die Migration
    # in chats_auflisten() griff sonst nach der ECHTEN Verlaufsdatei und
    # verschob sie ins Temp-Verzeichnis. Genau das ist am 23.08.2026
    # passiert - der bisherige Verlauf war weg. Ein Selbsttest darf
    # niemals echte Datenpfade anfassen.
    global VERLAUF_DATEI
    _chats_alt, _bilder_alt = CHATS_DIR, CHATBILDER_DIR
    _verlauf_alt = VERLAUF_DATEI
    with _tf.TemporaryDirectory() as ordner:
        CHATS_DIR = Path(ordner) / "chats"
        CHATBILDER_DIR = CHATS_DIR / "bilder"
        VERLAUF_DATEI = Path(ordner) / "kein_altbestand.jsonl"
        try:
            verlauf_anhaengen("user", "Wie ist das Wetter?", chat="probe-1")
            verlauf_anhaengen("assistant", "Mexla, sonnig.", "m", chat="probe-1",
                              zusatz={"bild": "kb_20260101_000000.jpg"})
            gelesen = verlauf_lesen(chat="probe-1")
            pruefe(len(gelesen) == 2 and gelesen[0]["role"] == "user",
                   "Nachrichten landen in ihrer Unterhaltung")
            pruefe(gelesen[1].get("bild") == "kb_20260101_000000.jpg",
                   "das gespeicherte Bild haengt an der Nachricht")
            pruefe(all("ts" in n for n in gelesen),
                   "jede Nachricht traegt einen Zeitstempel")
            verlauf_anhaengen("user", "Zweites Gespraech", chat="probe-2")
            liste = chats_auflisten()
            pruefe(len(liste) == 2 and liste[0]["id"] == "probe-2",
                   "die Liste zeigt beide, juengste zuerst", str(len(liste)))
            pruefe(liste[1]["titel"].startswith("Wie ist das Wetter"),
                   "der Titel kommt aus der ersten Frage", liste[1]["titel"])
            verlauf_leeren("probe-1")
            pruefe(verlauf_lesen(chat="probe-1") == []
                   and len(chats_auflisten()) == 1,
                   "geloescht wird nur die eine Unterhaltung")
        finally:
            CHATS_DIR, CHATBILDER_DIR = _chats_alt, _bilder_alt
            VERLAUF_DATEI = _verlauf_alt

    # --- aktion_starten: die Riegel vor dem Job-Server ---
    pruefe("Unzulaessiger Aktionsname" in
           werkzeug_ausfuehren("aktion_starten", {"name": "boese; rm -rf"}),
           "aktion_starten weist Namen mit Sonderzeichen ab")
    pruefe("Unzulaessiges Argument" in
           werkzeug_ausfuehren("aktion_starten",
                               {"name": "lampen", "argument": "a b"}),
           "aktion_starten weist Argumente mit Leerzeichen ab")
    pruefe("Unzulaessiger Aktionsname" in
           werkzeug_ausfuehren("aktion_starten", {"name": ""}),
           "aktion_starten ohne Namen wird abgewiesen")

    # --- Das Auge im Chat: Fakten vorlegen statt Halluzination ---
    text_an = auge_fuer_chat({"an": True, "gesehen": [
        {"deutsch": "Mensch", "vertrauen": 0.95},
        {"name": "chair", "vertrauen": 0.5}]})
    pruefe("AN" in text_an and "Mensch (95%)" in text_an
           and "chair (50%)" in text_an,
           "Chat-Rolle bekommt die aktuellen Funde vorgelegt", text_an[:90])
    text_aus = auge_fuer_chat({"an": False})
    pruefe("AUSGESCHALTET" in text_aus and "Auge" in text_aus,
           "bei ausgeschaltetem Auge steht das ehrlich drin")
    text_leer = auge_fuer_chat({"an": True, "gesehen": []})
    pruefe("noch nichts Bestaendiges" in text_leer,
           "ohne Funde wird nichts erfunden")

    # Port 0: das Betriebssystem sucht einen freien - so kollidiert der
    # Test nie mit einer laufenden Zentrale.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    basis = f"http://127.0.0.1:{port}"

    def anfrage(pfad, token=TOKEN, methode="GET", koerper=None):
        daten = json.dumps(koerper).encode("utf-8") if koerper is not None else None
        a = urllib.request.Request(basis + pfad, data=daten, method=methode)
        if token is not None:
            a.add_header("X-M1-Token", token)
        a.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(a, timeout=10) as antwort:
                return antwort.status, antwort.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:
            return -1, str(e)

    try:
        # --- Token ---
        code, _ = anfrage("/api/zustand", token=None)
        pruefe(code == 401, "ohne Token abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/api/zustand", token="falsch")
        pruefe(code == 401, "mit falschem Token abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/api/berichte")
        pruefe(code == 200, "mit richtigem Token durchgelassen", f"HTTP {code}")
        # --- Benchmark-Reiter (23.08.2026) ---
        # Der Endpunkt buendelt nur Lesendes; gestartet wird ueber
        # /api/start -> Positivliste. Haengt er, ist der Reiter leer.
        code, text = anfrage("/api/benchmark")
        try:
            bench = json.loads(text)
        except ValueError:
            bench = {}
        pruefe(code == 200 and "stand" in bench and "berichte" in bench
               and "quellen" in bench,
               "/api/benchmark liefert Stand, Berichte und Quellen",
               f"HTTP {code}")
        pruefe("fehler" not in bench,
               "/api/benchmark kommt an die Harness-Bausteine",
               str(bench.get("fehler", "")))
        code, _ = anfrage("/api/benchmark", token=None)
        pruefe(code == 401, "/api/benchmark ohne Token abgewiesen",
               f"HTTP {code}")
        html = (Path(__file__).parent / "zentrale.html").read_text(
            encoding="utf-8", errors="replace")
        pruefe('data-ansicht="benchmark"' in html
               and "zeigeBenchmark" in html,
               "zentrale.html hat den Benchmark-Reiter")
        # --- Werkzeuge des Chats: nur lesen, niemals ausfuehren ---
        namen = {w["function"]["name"] for w in CHAT_WERKZEUGE}
        for neu_name in ("kamerabild", "aktionen_zeigen", "aktion_starten"):
            pruefe(neu_name in namen, f"Chat-Werkzeug vorhanden: {neu_name}")
        kb = next(w["function"]["description"] for w in CHAT_WERKZEUGE
                  if w["function"]["name"] == "kamerabild")
        pruefe("Screenshot" in kb and "Foto" in kb,
               "kamerabild nennt die Synonyme (Screenshot, Foto) - sonst "
               "verweigert das Modell bei anderer Wortwahl")
        namen -= {"kamerabild", "aktionen_zeigen", "aktion_starten"}
        pruefe(namen == {"websuche", "webseite_lesen", "systemzustand",
                         "ablaeufe_zeigen", "berichte_lesen",
                         "projekte_auflisten", "projektdatei_lesen"},
               "Chat hat genau die sieben lesenden Werkzeuge", str(sorted(namen)))
        # --- Werkzeugrunden: gesprochen muss frueher Schluss sein ---
        # Der Sprachassistent wartet 300 s auf /api/chat; volle Runden
        # sprengen das Fenster (23.08.2026: 7 Minuten Stille auf "büro
        # rot"). Geprueft wird die Verdrahtung gegen einen Ollama-
        # Doppelgaenger, der immer weiter Werkzeuge verlangt: gesprochen
        # muessen genau 2 Modellaufrufe kommen (1 Runde + erzwungener
        # Abschluss ohne Werkzeuge), getippt die vollen Runden. Ein
        # stil-Parameter, der nur dekoriert statt zu begrenzen, faellt
        # hier auf.
        global OLLAMA
        gezaehlt = []

        class _OllamaProbe(BaseHTTPRequestHandler):
            def do_POST(self):
                laenge = int(self.headers.get("Content-Length", "0"))
                koerper = json.loads(self.rfile.read(laenge) or b"{}")
                gezaehlt.append("tools" in koerper)
                if "tools" in koerper:
                    inhalt = {"message": {
                        "role": "assistant", "content": "",
                        "tool_calls": [{"function": {
                            "name": "probe_werkzeug", "arguments": {}}}]}}
                else:
                    inhalt = {"message": {"role": "assistant",
                                          "content": "Probe fertig."}}
                roh = json.dumps(inhalt).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(roh)))
                self.end_headers()
                self.wfile.write(roh)

            def log_message(self, format, *args):  # noqa: A002
                pass

        probe_ollama = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaProbe)
        threading.Thread(target=probe_ollama.serve_forever,
                         daemon=True).start()
        ollama_echt = OLLAMA
        try:
            OLLAMA = "http://127.0.0.1:%d" % probe_ollama.server_address[1]
            gesprochen = chat_anfragen(
                "probe", [{"role": "user", "content": "büro rot"}],
                stil="sprache")
            pruefe(gezaehlt == [True, False],
                   "gesprochen: 1 Werkzeugrunde, dann Abschluss ohne "
                   "Werkzeuge", str(gezaehlt))
            pruefe(gesprochen.get("antwort") == "Probe fertig.",
                   "gesprochen: die Abschlussantwort kommt an",
                   str(gesprochen)[:60])
            gezaehlt.clear()
            chat_anfragen("probe", [{"role": "user", "content": "probe"}])
            pruefe(len(gezaehlt) == CHAT_WERKZEUG_RUNDEN + 1
                   and gezaehlt[-1] is False,
                   "getippt: volle Werkzeugrunden bleiben erhalten",
                   str(gezaehlt))
        finally:
            OLLAMA = ollama_echt
            probe_ollama.shutdown()
        # --- Modelltest-Dialog im Prompt (23.08.2026) ---
        # Mexlas Vorgabe: Vor einem Benchmark fragt Tim erst, ob er nach
        # neuen Tests suchen soll; bei nein startet er direkt. Der
        # Abschnitt muss die Rueckfrage und die echten Aktionsnamen
        # nennen - ein Tippfehler hier hiesse: Tim ruft eine Aktion auf,
        # die es nicht gibt, und der Dialog stirbt beim ersten Versuch.
        pruefe("Soll ich vorher nach neuen Benchmark-Tests suchen?"
               in SYSTEM_PROMPT,
               "Prompt enthaelt die Benchmark-Rueckfrage")
        for aktion in ("modell_benchmark_neue", "modell_benchmark_modell",
                       "modell_benchmark_status", "modell_benchmark_vergleich",
                       "benchmark_faelle_uebernehmen"):
            pruefe(aktion in SYSTEM_PROMPT,
                   f"Prompt nennt die Aktion {aktion}")
        # --- Diagnose-Dialog im Prompt (24.08.2026) ---
        # Tim soll bei "ist etwas kaputt?" pruefen statt raten und vor
        # jeder Veroeffentlichung die Datenschutz-Pruefung verlangen.
        # Ein Tippfehler in den Aktionsnamen hiesse: Tim ruft eine
        # Aktion auf, die es nicht gibt, und der Dialog stirbt.
        for aktion in ("ha_diagnose", "doppelablage_pruefen",
                       "datenschutz_pruefen"):
            pruefe(aktion in SYSTEM_PROMPT,
                   f"Prompt nennt die Diagnose-Aktion {aktion}")
        pruefe("PRUEFE mit aktion_starten, statt" in SYSTEM_PROMPT,
               "Prompt verlangt Pruefen statt Vermuten")
        pruefe("docs/TIM_HANDWERK.md" in SYSTEM_PROMPT,
               "Prompt verweist auf das Handwerks-Heft")
        pruefe("Puls-Geber" in SYSTEM_PROMPT,
               "Prompt kennt die Puls-Geber-Kacheln (aus ist richtig)")
        pruefe("VOR JEDER VEROEFFENTLICHUNG" in SYSTEM_PROMPT,
               "Prompt macht die Datenschutz-Pruefung zur Pflicht")
        pruefe("Veroeffentlichen (Commit, Push)" in SYSTEM_PROMPT
               and "kannst du selbst NICHT" in SYSTEM_PROMPT,
               "Prompt zieht die Grenze: pruefen ja, veroeffentlichen nein")
        # --- Selbstauskunft ueber die eigene Anlage (23.08.2026) ---
        # Tim erklaerte Mexla, die Lampen-Transportlogik stehe in
        # harness/jobs/*.json, und der Weg zur Lampe liege "ausserhalb
        # meiner Reichweite" - beides falsch: die Kette steht in den
        # Unterlagen, die er mit projektdatei_lesen selbst lesen kann.
        # Er hatte das Werkzeug und riet trotzdem. Diese Bausteine
        # halten die Antwort im Prompt.
        for baustein in ("Pico W", "lampen_steuern.py",
                         "Bluetooth-Rundruf", "docs/LAMPEN_BRMESH.md",
                         "hardware/pico_bruecke/README.md"):
            pruefe(baustein in SYSTEM_PROMPT,
                   f"Prompt erklaert die Lampenkette: {baustein}")
        pruefe("keiner braucht den anderen" in SYSTEM_PROMPT.lower(),
               "Prompt nennt HA und Tim als gleichberechtigte Clients")
        pruefe("bruecke_wlan.py" in SYSTEM_PROMPT,
               "Prompt kennt die WLAN-Bruecke (V2 seit 23.08.2026)")
        # Tim sagte am 23.08. noch, der Pico "haengt direkt am USB-Port".
        # Das USB-Kabel ist seit V2 nur noch Wartungsweg.
        pruefe("NICHT am Mac" in SYSTEM_PROMPT,
               "Prompt stellt klar: Pico haengt nicht mehr am Mac")
        pruefe("KEIN Transportweg fuer Hardware" in SYSTEM_PROMPT,
               "Prompt trennt Ablaeufe von der Hardware-Kette")
        # Die allgemeine Lehre, nicht nur der Lampenfall: bei Fragen zur
        # eigenen Anlage erst nachlesen, dann reden.
        pruefe("projektdatei_lesen, BEVOR du ueber dich selbst sprichst"
               in SYSTEM_PROMPT,
               "Prompt verlangt Nachlesen statt Raten ueber sich selbst")
        # Der Dateizugriff des Chats muss auf Mexlas Projektordner begrenzt
        # bleiben - und versteckte Dateien (.env, .ssh, .git) ausblenden.
        for tabu in ("~/.zshrc", "/etc/passwd", "~/.ssh/id_rsa",
                     "~/Desktop/M1_DEPLOYMENT/.git/config",
                     "~/.m1_job_token"):
            ergebnis = werkzeug_ausfuehren("projektdatei_lesen", {"pfad": tabu})
            pruefe(ergebnis.startswith("Zugriff verweigert")
                   or ergebnis.startswith("Nicht gefunden"),
                   f"Chat liest nicht: {tabu}", ergebnis[:60])
        pruefe(len(CHAT_PROJEKTORDNER) <= 3
               and all("Desktop" in p for p in CHAT_PROJEKTORDNER),
               "Projektfreigabe bleibt auf wenige Desktop-Ordner begrenzt",
               str(CHAT_PROJEKTORDNER))
        # Die Werkzeuge, die auf diesen Mac schauen, duerfen nur lesen -
        # und der Berichtsleser nicht aus berichte/ ausbrechen.
        for boese in ("../../../etc/passwd", "..%2Fetc%2Fpasswd", "/etc/passwd",
                      "bericht.md/../../../etc/passwd", ".zshrc"):
            ergebnis = werkzeug_ausfuehren("berichte_lesen", {"name": boese})
            pruefe(ergebnis.startswith("Fehler:") and "root:" not in ergebnis,
                   f"Berichtsleser bricht nicht aus: {boese[:32]}")
        pruefe("Kill-Switch" in werkzeug_ausfuehren("systemzustand", {}),
               "Systemzustand liefert den Kill-Switch-Stand")
        for verboten in ("shell", "ablauf_starten", "datei_schreiben",
                         "notaus", "autonomie_setzen"):
            pruefe(verboten not in namen,
                   f"Chat hat KEIN Werkzeug '{verboten}'")
            pruefe(werkzeug_ausfuehren(verboten, {}).startswith("Unbekanntes"),
                   f"Aufruf von '{verboten}' wird abgewiesen")
        # Der Seitenabruf des Chats muss dieselben Sperren haben wie der
        # des Harness - sonst waere ueber den Chat erreichbar, was dort
        # bewusst gesperrt ist.
        for innen in ("http://127.0.0.1:8765/aktionen",
                      "http://100.100.100.100:8770/",
                      "file:///etc/passwd"):
            pruefe(werkzeug_ausfuehren("webseite_lesen", {"adresse": innen})
                   .startswith("Abruf abgelehnt"),
                   f"Chat erreicht innere Adresse nicht: {innen[:38]}")

        code, _ = anfrage("/api/sprachlog", token=None)
        pruefe(code == 401, "Sprachprotokoll ohne Token abgewiesen",
               f"HTTP {code}")
        code, _ = anfrage("/api/sprachlog")
        pruefe(code == 200, "Sprachprotokoll mit Token lesbar", f"HTTP {code}")

        # Die Oberflaeche selbst traegt keine Daten und ist absichtlich frei.
        code, _ = anfrage("/", token=None)
        pruefe(code == 200, "Oberflaeche auch ohne Token erreichbar",
               f"HTTP {code}")

        # --- Ausbruch aus dem Berichtsordner ---
        for name in ("../../../etc/passwd", "../.zshrc", "..%2F..%2Fetc%2Fpasswd",
                     "/etc/passwd", "bericht.md/../../../etc/passwd"):
            code, text = anfrage("/api/bericht?name=" + name)
            pruefe(code in (400, 404) and "root:" not in text,
                   f"Ausbruchsversuch abgewehrt: {name[:34]}", f"HTTP {code}")

        # --- Namensriegel des Berichtslesers ---
        # Die Ausbruchsversuche oben zeigen NICHT, dass der Namensriegel
        # wirkt: sie scheitern schon an der zweiten Huerde, dass die
        # Datei ausserhalb von berichte/ liegt. Deckung entsteht erst
        # mit Faellen, die INNERHALB von berichte/ landen und trotzdem
        # nichts im Chat verloren haben - dort ist der Riegel das
        # einzige, was sie aufhaelt. Ohne diesen Block lief die Mutation
        # "Berichtsleser laesst beliebige Pfade zu" am 23.08.2026
        # unbemerkt durch: ein Sicherheitsfix ohne Test.
        #
        # Geprueft wird gegen einen eigenen Probeordner statt gegen die
        # echten Berichte - der Selbsttest legt sonst Dateien im
        # laufenden Betrieb an.
        global BERICHTE_DIR
        echte_berichte = BERICHTE_DIR
        probe = Path(tempfile.mkdtemp(prefix="m1_berichte_probe_")).resolve()
        try:
            BERICHTE_DIR = probe
            (probe / "bericht.md").write_text("# Probe\nKENNUNG-4711\n",
                                              encoding="utf-8")
            # Liegt im Ordner und ist lesbar - nur die Endung haelt es auf.
            (probe / "geheim.txt").write_text("KENNUNG-4711\n",
                                              encoding="utf-8")
            # Bleibt unter berichte/, also greift die Ordnerpruefung
            # nicht - nur der Schraegstrich im Namen.
            (probe / "unter").mkdir()
            (probe / "unter" / "heimlich.md").write_text("KENNUNG-4711\n",
                                                         encoding="utf-8")
            for name in ("geheim.txt", "unter/heimlich.md",
                         str(probe / "bericht.md"),
                         str(probe / "geheim.txt")):
                ergebnis = werkzeug_ausfuehren("berichte_lesen", {"name": name})
                pruefe(ergebnis.startswith("Fehler:")
                       and "KENNUNG-4711" not in ergebnis,
                       f"Chat-Berichtsleser weist ab: {name[-34:]}",
                       ergebnis[:60])
                code, text = anfrage("/api/bericht?name=" + quote(name, safe=""))
                pruefe(code in (400, 404) and "KENNUNG-4711" not in text,
                       f"HTTP-Berichtsleser weist ab: {name[-34:]}",
                       f"HTTP {code}")
            # Gegenprobe: ein sauberer Name MUSS durchkommen. Sonst
            # bestuende dieser Block auch dann, wenn der Leser gar nichts
            # mehr liefert - und waere wieder nur Dekoration.
            ergebnis = werkzeug_ausfuehren("berichte_lesen", {"name": "bericht.md"})
            pruefe("KENNUNG-4711" in ergebnis,
                   "sauberer .md-Name kommt beim Chat durch", ergebnis[:60])
            code, text = anfrage("/api/bericht?name=bericht.md")
            pruefe(code == 200 and "KENNUNG-4711" in text,
                   "sauberer .md-Name kommt ueber HTTP durch", f"HTTP {code}")
        finally:
            BERICHTE_DIR = echte_berichte
            shutil.rmtree(str(probe), ignore_errors=True)

        # --- Argument- und Aktionsfilter ---
        for arg in ("modell_scan; touch /tmp/uebernommen", "a b", "../evil",
                    "$(whoami)", "a|b"):
            code, _ = anfrage("/api/start", methode="POST",
                              koerper={"aktion": "ablauf_starten", "argument": arg})
            pruefe(code == 400, f"Argument abgewiesen: {arg[:30]}", f"HTTP {code}")
        code, _ = anfrage("/api/start", methode="POST",
                          koerper={"aktion": "rm -rf /", "argument": ""})
        pruefe(code == 400, "Aktion mit Sonderzeichen abgewiesen", f"HTTP {code}")

        # --- Zuhoeren: Token und Formatpruefung ---
        code, _ = anfrage("/api/hoeren", token=None, methode="POST")
        pruefe(code == 401, "Aufnahme ohne Token abgewiesen", f"HTTP {code}")
        # Nicht-WAV darf nicht bis whisper-cli durchkommen.
        a = urllib.request.Request(basis + "/api/hoeren", data=b"keinwav",
                                   method="POST")
        a.add_header("X-M1-Token", TOKEN)
        a.add_header("Content-Type", "audio/wav")
        try:
            with urllib.request.urlopen(a, timeout=10) as antwort:
                roh = json.loads(antwort.read().decode("utf-8"))
            pruefe("fehler" in roh, "Nicht-WAV wird abgewiesen", str(roh)[:60])
        except (urllib.error.URLError, OSError, ValueError) as e:
            pruefe(False, "Nicht-WAV wird abgewiesen", str(e))

        # --- Unterhaltungen ueber HTTP ---
        code, _ = anfrage("/api/verlauf?chat=../geheim")
        pruefe(code == 400, "Verlauf mit boeser Kennung abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/api/chatbild?name=../../etc/passwd&token=" + TOKEN)
        pruefe(code == 400, "Chatbild mit boesem Namen abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/api/chatbild?name=kb_20990101_000000.jpg",
                          token=None)
        pruefe(code == 401, "Chatbild ohne Token abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/api/verlauf/leeren", methode="POST",
                          koerper={"chat": "../geheim"})
        pruefe(code == 400, "Leeren mit boeser Kennung abgewiesen", f"HTTP {code}")

        # --- Unbekannte Pfade ---
        code, _ = anfrage("/api/gibtsnicht")
        pruefe(code == 404, "unbekannter Pfad abgewiesen", f"HTTP {code}")

        # --- Kill-Switch blockiert das Starten ---
        # killswitch_aktiv wird ersetzt statt eine echte STOP-Datei
        # anzulegen: der Test darf den laufenden Betrieb nicht stoppen.
        global killswitch_aktiv
        echt = killswitch_aktiv
        killswitch_aktiv = lambda: "/opt/ki-server/STOP"
        try:
            code, _ = anfrage("/api/start", methode="POST",
                              koerper={"aktion": "status", "argument": ""})
            pruefe(code == 423, "bei gesetztem Kill-Switch kein Start",
                   f"HTTP {code}")
        finally:
            killswitch_aktiv = echt

        # --- Gleichlauf mit autonomie.py ---
        # Weicht die Liste ab, meldet die Zentrale "frei", waehrend der
        # Harness laengst gestoppt ist. Genau diese Abweichung gab es im
        # Sicherheits-Review vom 18.08.2026 in drei von sechs Dateien.
        try:
            if str(HARNESS_DIR) not in sys.path:
                sys.path.insert(0, str(HARNESS_DIR))
            import autonomie
            meine = {str(o / "STOP") for o in STOP_ORTE}
            seine = {str(p) for p in autonomie.STOP_KANDIDATEN}
            fehlend = seine - meine
            pruefe(not fehlend, "Kill-Switch-Orte decken autonomie.py ab",
                   f"fehlen: {sorted(fehlend)}")
        except ImportError as e:
            pruefe(False, "autonomie.py fuer den Gleichlauf ladbar", str(e))

    finally:
        server.shutdown()
        server.server_close()

    if fehler:
        print(f"\n{fehler} Fehler.")
    return fehler


def main() -> None:
    if "--selbsttest" in sys.argv[1:]:
        sys.exit(_selbsttest())

    # M1_ZENTRALE_HOST nimmt eine oder mehrere Adressen, durch Komma
    # getrennt. Standard ist nur das Loopback.
    #
    # Fuer den Zugriff vom Laptop oder Handy kommt die Tailscale-Adresse
    # dazu - bewusst als zweite Bindung und nicht als 0.0.0.0: sonst
    # haengt die Zentrale auch im heimischen WLAN, wo sie nichts zu
    # suchen hat. Dieselbe Ueberlegung wie bei Odysseus und Open WebUI,
    # die Docker mit zwei -p Angaben loest.
    adressen = [a.strip() for a
                in os.environ.get("M1_ZENTRALE_HOST", "127.0.0.1").split(",")
                if a.strip()]
    port = int(os.environ.get("M1_ZENTRALE_PORT", "8770"))

    server_liste = []
    for adresse in adressen:
        try:
            server_liste.append(ThreadingHTTPServer((adresse, port), Handler))
            print(f"M1-Zentrale laeuft auf http://{adresse}:{port}")
        except OSError as e:
            # Eine unerreichbare Adresse (Tailscale aus, IP gewechselt)
            # darf die uebrigen nicht mitreissen.
            print(f"  {adresse}:{port} nicht belegbar ({e}) - uebersprungen")

    if not server_liste:
        print("Keine einzige Adresse belegbar - Abbruch.")
        return

    print(f"Token: {TOKEN_DATEI}  (im Browser einmal eingeben)")
    if not OBERFLAECHE.exists():
        print(f"WARNUNG: {OBERFLAECHE} fehlt - die Oberflaeche bleibt leer.")
    stop = killswitch_aktiv()
    if stop:
        print(f"Hinweis: Kill-Switch ist gesetzt ({stop}) - "
              f"Ablaeufe lassen sich nicht starten.")

    for server in server_liste[:-1]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server_liste[-1].serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
