#!/usr/bin/env python3
"""
Lokaler Sprachassistent: Mikrofon -> Whisper -> gpt-oss (Ollama) -> Sprachausgabe
Wake Word: "Hey Tim"

Hinweis zur Wake-Word-Erkennung: Es laeuft (noch) kein echtes Always-On
Wake-Word-Modell (openwakeword ist installiert, aber es gibt noch kein
trainiertes "Hey Tim"-Modell dafuer). Stattdessen wird staendig in kurzen
Schnipseln mit dem SCHNELLEN Whisper-Tiny-Modell transkribiert und nach
dem Wake Word gesucht - das ist schneller als vorher (Tiny statt Medium,
2s statt 3s Schnipsel), aber immer noch kein Chip-Wake-Word wie bei
Alexa/Siri. Nach Erkennung wechselt es fuer den eigentlichen Befehl auf
das genauere Medium-Modell.
"""

import sounddevice as sd
import numpy as np
import subprocess
import requests
import threading
import tempfile
import os
import collections
import json
import re
import difflib
import glob
import queue
import shutil
import sys
import time

# launchd startet Dienste mit minimalem PATH - whisper-cli (Homebrew) und
# docker waeren dann unauffindbar, und der Assistent wuerde nur als Dienst
# scheitern, im Terminal aber laufen. Gleicher Kniff wie im Job-Server.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

# --dienst: Dauerbetrieb unter launchd. Unterschiede zum Terminal-Start:
# auf ein Mikrofon warten statt aufgeben, bei Kill-Switch ruhen statt
# beenden (launchd wuerde den Prozess sonst sofort neu starten).
DIENST_MODUS = "--dienst" in sys.argv

# --- Konfiguration ---
OLLAMA_URL = "http://localhost:11434/api/generate"
# Dasselbe Modell und dieselbe Kontextgroesse wie Tims Browser-Chat: so
# bleibt EIN grosses Modell warm im Speicher, statt dass Sprache und Chat
# sich gegenseitig verdraengen. qwen3-general stand hier vorher - dessen
# Modelfile hat aber eine kaputte Vorlage (TEMPLATE {{ .Prompt }}), das
# Modell denkt deshalb als Fliesstext laut vor sich hin, und genau dieses
# Denken wurde dann komplett vorgelesen. Danach stand hier gpt-oss:20b -
# im Benchmark vom 23.08.2026 lieferte es aber zweimal nach minutenlangem
# Denken eine LEERE Antwort, und am Sprachweg heisst leer: Tim schweigt.
# qwen3.5:9b: 14/14 Pruefungen, 30.5 Tok/s, 4.8 s Ladezeit, entspricht
# STANDARD_MODELL der Zentrale (m1_zentrale.py).
OLLAMA_MODEL = "gpt-oss:20b"
MODELL_NUM_CTX = 16384           # wie CHAT_NUM_CTX in m1_zentrale.py
# Der gesprochene Weg geht ueber Tims eigenen Chat - so kann er dieselben
# Werkzeuge (Websuche, Seite lesen) und bekommt kuenftige von selbst mit.
ZENTRALE_URL = "http://127.0.0.1:8770"
PIPER_BIN = "/opt/ki-server/piper/piper"
PIPER_VOICE = "/opt/ki-server/piper/voices/de_DE-thorsten-high.onnx"
# Eingebaute macOS-Stimme als Rueckfallebene. Auswahl aller deutschen
# Stimmen mit:  say -v "?" | grep de_DE   (Anna, Eddy, Flo, Reed, ...)
MAC_STIMME = "Anna"
SAMPLE_RATE = 16000
# Whisper schreibt "Hey Tim" je nach Aussprache unterschiedlich. Lieber ein
# paar Varianten zu viel als ein Assistent, der nie reagiert.
WAKE_WORDS = ["hey tim", "hey team", "hey timm", "heytim", "hey tim.",
              "hei tim", "ey tim", "hey thim", "hey tin", "hey team.",
              "hey tim,", "hey team,",
              # so schreibt Whisper es, wenn direkt ein Satz folgt:
              "haeltim", "haelt im", "hältim", "hält im",
              "helpt im", "heltim", "helt im", "haltim", "halt im",
              # so kam ein angeschnittenes "Hey Tim" am 22.08.2026 an -
              # als Netz, falls die Ueberlappung einmal nicht greift:
              "helik tim", "heli tim", "hey team", "herr tim", "her tim",
              "hallo tim", "he tim"]
# Homebrew-Paket "whisper-cpp" liefert das Programm als "whisper-cli";
# aeltere Fassungen hiessen "whisper-cpp". Beide werden akzeptiert.
WHISPER_BIN = shutil.which("whisper-cli") or shutil.which("whisper-cpp") or "whisper-cli"
WAKE_MODEL = "/opt/ki-server/whisper-models/ggml-base.bin"      # fuers Zuhoeren
# ggml-tiny war hier zu ungenau ("kanzler" statt "kannst du"). base ist
# rund doppelt so gross, auf Apple Silicon aber immer noch in Sekunden
# durch - und trifft das Weckwort deutlich zuverlaessiger.
COMMAND_MODEL = "/opt/ki-server/whisper-models/ggml-medium.bin"  # genauer, fuer den Befehl
MAX_VERLAUF = 5  # wie viele letzte Frage/Antwort-Paare als Gespraechs-Kontext mitgeschickt werden
# Aufnahme in Bloecken mit Stille-Erkennung waere schneller, lieferte am
# 22.08.2026 aber nachweislich schlechteres Audio (siehe
# befehl_aufnehmen). Erst wieder einschalten, wenn ein Mitschnitt zeigt,
# dass die Blocktechnik dieselbe Erkennungsrate hat wie eine Aufnahme am
# Stueck.
STILLE_ERKENNUNG = False

verlauf = []  # einfaches Konversationsgedaechtnis: Liste von (frage, antwort)


# --- Kill-Switch -------------------------------------------------------
# Dieser Assistent ist ein autonomer Pfad: eine Endlosschleife, die
# dauerhaft mithoert und jede erkannte Aeusserung an das Modell schickt.
# Bis Runde 3 pruefte er den Kill-Switch gar nicht - "m1-stop" meldete
# Erfolg, waehrend das Mikrofon weiterlief. Dieselbe Ortsliste wie in
# harness/autonomie.py (STOP_KANDIDATEN).
STOP_ORTE = [
    "/opt/ki-server/STOP",
    os.path.expanduser("~/Desktop/M1_DEPLOYMENT/STOP"),
    "/Volumes/M1_DEPLOYMENT/STOP",
    "/Volumes/Extreme SSD/M1_DEPLOYMENT/STOP",
    "/Volumes/SanDisk/M1_DEPLOYMENT/STOP",
    "/Volumes/SANDISK/M1_DEPLOYMENT/STOP",
]


def killswitch_aktiv():
    """Pfad der STOP-Datei, falls vorhanden - sonst None."""
    for p in STOP_ORTE + glob.glob("/Users/*/Desktop/M1_DEPLOYMENT/STOP"):
        if os.path.exists(p) or os.path.islink(p):
            return p
    return None


def aufnehmen(sekunden):
    """Nimmt Audio vom gewaehlten Mikrofon auf (None = Systemstandard)."""
    audio = sd.rec(int(sekunden * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                    channels=1, dtype='float32', device=_mik_index)
    sd.wait()
    return audio.flatten()


# ----------------------------------------------------------------------
# Ringpuffer - das Verfahren, mit dem Siri und Alexa dauerhaft mithoeren
# ----------------------------------------------------------------------
# Bis 22.08.2026 lief es in Fenstern: 3 Sekunden aufnehmen, Mikrofon zu,
# auswerten, Mikrofon auf. Faellt "Hey Tim" in die Auswertepause, ist es
# weg - belegt durch eingespielte Weckworte, die als "helik tim",
# "periktym", "peritum" ankamen. Ueberlappende Fenster halfen nur
# teilweise.
#
# Jetzt: EIN Aufnahmestrom, der nie schliesst, schreibt fortlaufend in
# einen Ringpuffer. Geprueft wird immer der jeweils letzte Ausschnitt.
# Es gibt keine Pause mehr, in der etwas verlorengehen kann. Zusaetzlich
# liegt dadurch das, was NACH dem Weckwort gesagt wurde, bereits im
# Puffer - "Hey Tim, starte den Selbsttest" in einem Atemzug funktioniert
# damit wie bei den grossen Systemen.
PUFFER_BLOCK = 0.1          # Groesse der Bloecke, die der Strom liefert
PUFFER_SEKUNDEN = 12        # so weit reicht das Gedaechtnis zurueck
PRUEFTAKT = 1.0             # so oft wird der Puffer aufs Weckwort geprueft
# Wie lange der Aufnahmestrom laeuft, bevor er planmaessig erneuert wird.
# Grund (22.08.2026 belegt): Wird die Webcam im Betrieb abgezogen, wirft
# PortAudio keinen Fehler - der Strom liefert weiter Rauschen, und Tim
# "hoert" Halluzinationen von einem Geraet, das gar nicht mehr da ist.
# Beim Erneuern wird das Mikrofon neu gewaehlt; die Luecke von
# Sekundenbruchteilen alle drei Minuten faellt nicht ins Gewicht.
STROM_ERNEUERN = 180.0
PRUEFFENSTER = 3.0          # so weit zurueck wird dabei gehoert
NACHLAUF = 2.5              # nach dem Weckwort noch ausreden lassen


def puffer_anlegen(sekunden=PUFFER_SEKUNDEN):
    """Leerer Ringpuffer. deque wirft alte Bloecke von selbst hinaus."""
    return collections.deque(maxlen=int(sekunden / PUFFER_BLOCK))


def puffer_letzte(puffer, sekunden):
    """Die letzten n Sekunden aus dem Puffer, in richtiger Reihenfolge."""
    bloecke = list(puffer)          # Momentaufnahme, waehrend der Strom weiterschreibt
    if not bloecke:
        return np.zeros(0, dtype="float32")
    audio = np.concatenate(bloecke)
    return audio[-int(sekunden * SAMPLE_RATE):]


def befehl_aufnehmen(max_sekunden=8):
    """Nimmt auf, bis der Sprecher fertig ist - statt stur 8 Sekunden.

    Die feste Aufnahmezeit war der groesste Zeitfresser (gemessen am
    21.08.2026: Whisper 1,4s, Modell warm 2s - die Aufnahme dominierte).

    WICHTIG: ein einziger durchgehender Audiostrom. Die erste Fassung
    nahm die 0,3s-Bloecke mit einzelnen sd.rec()-Aufrufen auf - zwischen
    den Aufrufen ging beim Schliessen und Neuoeffnen des Stroms jedes Mal
    ein Stueck Audio verloren, und Whisper bekam zerhacktes Kauderwelsch.
    Das Weckwort funktionierte weiter, weil es in EINEM Stueck
    aufgenommen wird - genau daran fiel der Fehler auf.

    Ende der Aufnahme: 1,2 Sekunden unter der Stilleschwelle, sobald
    vorher gesprochen wurde. Die Schwelle richtet sich nach dem
    Grundrauschen des ersten Blocks (Webcam-Mikros sind leiser als
    Headsets); bei Dauerlaerm greift die Obergrenze.
    """
    # 22.08.2026, nachgemessen: Ueber die Blocktechnik kam sogar ein
    # sauber eingespieltes "Hey Tim" als "helikton" an, waehrend
    # DASSELBE Audio mit einer durchgehenden sd.rec-Aufnahme wortgenau
    # erkannt wurde ("Wie viel ist 10 mal 3000?"). Sichere Aufnahme
    # schlaegt schnelle Aufnahme - deshalb hier eine Aufnahme am Stueck,
    # dieselbe Technik wie im Weckwort-Pfad, der zuverlaessig laeuft.
    # Die Stille-Erkennung bleibt als Code erhalten, ist aber ueber
    # STILLE_ERKENNUNG abgeschaltet, bis sie belegt sauber laeuft.
    if not STILLE_ERKENNUNG:
        return aufnehmen(sekunden=6)

    block = 0.3
    noetige_stille = 4       # 4 Bloecke = 1,2 Sekunden
    schlange = queue.Queue()

    def _rein(indata, frames, zeit, status):
        schlange.put(indata[:, 0].copy())

    teile = []
    gesprochen = False
    stille = 0
    schwelle = None
    try:
        # Rueckruf + Warteschlange: dieselbe Technik, mit der sd.rec im
        # Weckwort-Pfad seit Tagen fehlerfrei aufnimmt. Das blockierende
        # strom.read() lieferte im Dienstbetrieb die Daten mit halber
        # Geschwindigkeit ("15.4s aufnehmen" fuer 7.8s Audio im Protokoll)
        # - der Ton war auf Doppeltempo gestaucht, Whisper verstand nur
        # noch Kauderwelsch ("stoepelsaeuse" statt "stoppe Odysseus").
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32", device=_mik_index,
                            blocksize=int(block * SAMPLE_RATE),
                            callback=_rein):
            for _ in range(int(max_sekunden / block)):
                teil = schlange.get(timeout=3)
                teile.append(teil)
                pegel = float(np.sqrt(np.mean(teil ** 2)))
                if schwelle is None:
                    # Erster Block = Grundrauschen dieses Mikrofons.
                    # Spricht jemand sofort los, wird die Schwelle zu
                    # hoch - dann laeuft die Aufnahme einfach die vollen
                    # 8 Sekunden. Lieber das als zu frueh abschneiden.
                    schwelle = max(0.004, 3.0 * pegel)
                    continue
                if pegel >= schwelle:
                    gesprochen = True
                    stille = 0
                else:
                    stille += 1
                # Mindestens 2 Sekunden, egal wie still es scheint: bei
                # leiser Stimme oder hoher Schwelle darf die Aufnahme
                # nicht nach dem ersten Atemholen enden.
                if (gesprochen and stille >= noetige_stille
                        and len(teile) * block >= 2.0):
                    break
    except queue.Empty:
        print("Aufnahmestrom liefert nichts - Rueckfall auf feste Aufnahme.")
        return aufnehmen(sekunden=6)
    if not teile:
        return aufnehmen(sekunden=6)
    return np.concatenate(teile)


# Die letzte Befehlsaufnahme bleibt als WAV liegen. Ohne sie laesst sich
# nicht unterscheiden, ob das Mikrofon Mist liefert oder Whisper ihn baut -
# genau daran ging am 21./22.08.2026 die Fehlersuche mehrfach vorbei.
MITSCHNITT = "/opt/ki-server/logs/letzte_aufnahme.wav"

# Eigennamen und Fachwoerter, die Whisper von sich aus nicht sicher
# schreibt (es hoerte "startdüssel", "stoepelsaeuse"). Der Prompt ist ein
# Hinweis, kein Zwang.
WHISPER_PROMPT = ("Tim, Selbsttest, Modell-Scan, Autonomie, "
                  "Kill-Switch, Ablauf, Healthcheck, Funkbruecke.")


def transkribieren(audio, modell, mitschneiden=False, stichworte=False):
    """Wandelt Audio in Text um via whisper.cpp.

    stichworte=True gibt Whisper die Eigennamen mit. Das hilft beim
    Befehl ("starte den Modell-Scan"), schadet aber beim Weckwort-Lauschen:
    auf Rauschen antwortet Whisper dann mit den Stichworten selbst
    (belegt am 22.08.2026: "ablauf, ablauf, healthcheck").
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        import scipy.io.wavfile as wav
        wav.write(f.name, SAMPLE_RATE, (audio * 32767).astype(np.int16))
        wav_path = f.name
    if mitschneiden:
        try:
            shutil.copyfile(wav_path, MITSCHNITT)
        except OSError:
            pass

    befehl = [WHISPER_BIN, "-m", modell, "-l", "de", "-f", wav_path,
              "--no-timestamps"]
    if stichworte:
        befehl += ["--prompt", WHISPER_PROMPT]
    result = subprocess.run(befehl, capture_output=True, text=True)
    os.unlink(wav_path)
    text = result.stdout.strip().lower()

    # Whisper fuellt Stille und Rauschen mit Platzhaltern wie "[musik]",
    # "(applaus)" oder "[gelaechter]". Die sind kein gesprochener Text und
    # haben in der Weckwortpruefung nichts verloren.
    for anfang, ende in (("[", "]"), ("(", ")"), ("*", "*")):
        while anfang in text and ende in text[text.index(anfang):]:
            start = text.index(anfang)
            stop = text.index(ende, start) + 1
            text = (text[:start] + " " + text[stop:]).strip()
    return " ".join(text.split())


# Klangliche Weckwort-Erkennung statt fester Liste.
#
# Warum: Whisper schreibt "Hey Tim" je nach Aufnahme voellig anders -
# belegt am 22.08.2026: "peritim", "heritium", "helik tim", "herr tim",
# "heitem". Eine Liste kann das nie einholen; jede neue Aussprache
# faellt durch. Verglichen wird deshalb die AEHNLICHKEIT zu "heytim".
#
# Die Schwelle ist gemessen, nicht geschaetzt: gegen alle 146 bis dahin
# protokollierten Zeilen ergab 0.60 acht plausible Weckwort-Treffer und
# keinen echten Fehlalarm. Ein Fehlalarm waere ohnehin harmlos - Tim
# fragt dann "Ja?" und hoert nach.
WECKWORT_SCHWELLE = 0.60
WECKWORT_KLANG = "heytim"


def _klang_am_anfang(kandidat):
    """Faengt das Wort vorne an wie "heytim" - oder passt nur das Ende?

    Warum: Die reine Aehnlichkeit sieht nicht, WO sie sitzt. "nicht im"
    wird zu "nichtim", teilt sich mit "heytim" das "h" und das "tim" und
    kam damit auf 0.615 - knapp ueber der Schwelle. Am 22.08.2026 abends
    hat genau das Tim geweckt: er fragte "Ja?" und beantwortete danach
    einen Satz, den niemand an ihn gerichtet hatte.

    "Hey Tim" faengt aber immer mit dem H-Laut an. Whisper verhoert sich
    in der Mitte und am Ende (peritim, heritium, heitem, halthim), der
    Anfang bleibt. Verlangt wird deshalb: die erste Uebereinstimmung
    muss in den ersten beiden Zeichen liegen - auf beiden Seiten.

    Gemessen gegen alle 16203 protokollierten Zeilen: von 26 Treffern
    faellt genau einer weg, der Fehlalarm "nicht im". Alle echten
    Verhoerer bleiben.
    """
    bloecke = difflib.SequenceMatcher(None, kandidat,
                                      WECKWORT_KLANG).get_matching_blocks()
    return bool(bloecke) and bloecke[0].a <= 1 and bloecke[0].b <= 1


# Wieviele Zeichen der "tim"-Haelfte ein Treffer mindestens abdecken muss.
# "heytim" besteht aus zwei Haelften: dem Anruf "hey" und dem NAMEN "tim".
# Nur der Name macht den Zuruf zu einem Zuruf an Tim.
WECKWORT_NAME_ZEICHEN = 2


def _name_gedeckt(kandidat, mindestens=WECKWORT_NAME_ZEICHEN):
    """Steckt im Treffer auch der Name - oder nur das "hey"?

    Warum: _klang_am_anfang prueft nur, dass es vorne passt. Damit reicht
    ein "hey" plus irgendein kurzes Wort ueber die Schwelle, denn "hey"
    allein bringt schon drei von sechs Zeichen mit. Gemessen am
    22.08.2026: "hey ich" -> "heyich" kommt auf 0.667, "hey die" ->
    "heydie" ebenso - beide ueber der Schwelle von 0.60.

    Genau das ist am 22.08.2026 gegen 23:50 passiert. Aus einem Film lief
    "hey, ich bin hinterher gefahren bei dem machtball" - Tim hielt das
    fuer seinen Namen und erklaerte ungefragt in den Raum, was ein
    "Machtball" sei. Dasselbe Muster steht im Protokoll noch dreimal.

    Wer Tim ruft, sagt aber seinen NAMEN. Whisper verhoert sich am Anruf
    ("peritim", "heritium", "helik tim", "herr tim", "halt him") - das
    "tim" am Ende bleibt dabei erhalten. Verlangt werden deshalb
    mindestens zwei der drei Zeichen von "tim".

    Gemessen an allen 12 protokollierten Nachhoer-Texten: die 6 echten
    Weckrufe decken alle drei Zeichen ab, die 6 Fehlalarme hoechstens
    eines. Die Trennung ist damit vollstaendig, nicht knapp.
    """
    gedeckt = set()
    for block in difflib.SequenceMatcher(None, kandidat,
                                         WECKWORT_KLANG).get_matching_blocks():
        for versatz in range(block.size):
            gedeckt.add(block.b + versatz)
    # Zeichen 3 bis 5 von "heytim" sind das "tim".
    return len(gedeckt & {3, 4, 5}) >= mindestens


def weckwort_finden(norm):
    """Sucht das Weckwort in bereits normalisiertem Text.

    Rueckgabe: (start, ende) als Wortindizes, oder None. Mit den
    Indizes laesst sich abtrennen, was NACH dem Weckwort gesagt wurde.
    """
    woerter = norm.split()

    # Erst die bekannten Schreibweisen - schnell und eindeutig.
    for ww in WAKE_WORDS:
        anzahl = len(ww.split())
        for i in range(len(woerter) - anzahl + 1):
            if " ".join(woerter[i:i + anzahl]) == ww:
                return i, i + anzahl
        # auch als Teilstring, z.B. "heytim" ohne Leerzeichen
        for i, wort in enumerate(woerter):
            if ww.replace(" ", "") == wort:
                return i, i + 1

    # Dann klanglich: jedes Ein- und Zweiwortfenster vergleichen - aber
    # nur solche, die auch VORNE wie "heytim" anfangen UND den Namen
    # mitbringen. Beides zusammen, weil jede Haelfte allein zu wenig ist:
    # "nichtim" passt hinten, "heyich" passt vorne - keines ist ein Ruf.
    beste_naehe, beste_stelle = 0.0, None
    for i in range(len(woerter)):
        for anzahl in (1, 2):
            kandidat = "".join(woerter[i:i + anzahl])
            if len(kandidat) < 5 or not _klang_am_anfang(kandidat):
                continue
            if not _name_gedeckt(kandidat):
                continue
            naehe = difflib.SequenceMatcher(None, kandidat,
                                            WECKWORT_KLANG).ratio()
            if naehe > beste_naehe:
                beste_naehe, beste_stelle = naehe, (i, i + anzahl)
    if beste_naehe >= WECKWORT_SCHWELLE:
        return beste_stelle
    return None


def _normalisieren(text):
    """Satzzeichen raus, Umlaute vereinheitlicht, Leerzeichen glaetten.

    Whisper setzt Kommas und Punkte nach Gehoer: aus demselben Zuruf wird
    mal "hey tim.", mal "hey, tim." - und Letzteres fiel durch, weil die
    Weckwortliste nur ganze Varianten aufzaehlt. Verglichen wird deshalb
    ohne Satzzeichen; gesprochen und ans Modell geschickt wird das
    Original.

    Umlaute werden auf die ASCII-Schreibweise gebracht (ae, oe, ue, ss):
    Whisper schreibt "büro", die angelernte Raumliste kennt nur "buero" -
    am 23.08.2026 fiel "büro rot" deshalb durch die Lichtregel in den
    Chat, der drehte seine Werkzeugrunden, und Tim schwieg 7 Minuten.
    Regeln und Tabellen duerfen sich seitdem auf die ASCII-Schreibweise
    verlassen; der Selbsttest haelt fest, dass jede Umlaut-Schreibweise
    in der Befehlstabelle ihren ASCII-Zwilling hat.
    """
    for zeichen in ",.!?;:-":
        text = text.replace(zeichen, " ")
    # Whisper schreibt Prozentwerte als Zeichen ("60%"), die Regeln
    # suchen das Wort - belegt am 23.08.2026: "büro ist 60%" lief ins
    # Modell statt in die Lichtregel.
    text = text.replace("%", " prozent")
    for umlaut, ersatz in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
                           ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"),
                           ("ß", "ss")):
        text = text.replace(umlaut, ersatz)
    return " ".join(text.split())


SPRECH_ANWEISUNG = (
    "Du bist Tim, ein lokaler Sprachassistent auf einem Mac. Antworte auf "
    "Deutsch, kurz und in gesprochener Sprache - hoechstens drei Saetze, "
    "keine Listen, kein Markdown. WICHTIG: Du selbst kannst NICHTS "
    "ausfuehren, starten oder stoppen - echte Befehle erkennt eine feste "
    "Tabelle, BEVOR ein Satz dich erreicht. Klingt ein Satz nach einem "
    "Befehl (starten, stoppen, ausfuehren) oder ist er unverstaendlich, "
    "wurde er NICHT erkannt: Sage dann, dass du ihn nicht verstanden "
    "hast, und bitte, ihn deutlich zu wiederholen. Behaupte niemals, "
    "etwas ausgefuehrt zu haben oder auszufuehren."
)


def _denken_entfernen(text):
    """Schickt ein Modell sein Nachdenken doch in <think>-Marken mit,
    wird nur der Teil danach vorgelesen."""
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = text.strip()
    # Die Ankerphrase "Mexla," ist die Drift-Erkennung des Textchats -
    # vorgelesen klingt sie nur seltsam. Sie steht im Modelfile UND im
    # System-Prompt; eine Bitte, sie wegzulassen, befolgt das Modell
    # nicht zuverlaessig. Deshalb wird sie hier abgeschnitten, wo sie
    # sicher wegkommt. Im Protokoll bleibt die volle Antwort stehen.
    for anrede in ("Mexla,", "Mexla:", "Mexla"):
        if text.startswith(anrede):
            text = text[len(anrede):].lstrip()
            break
    return text.strip()


def _token_lesen():
    """Das Token der Zentrale - dieselbe Datei, die auch Tim nutzt."""
    try:
        with open(os.path.expanduser("~/.m1_job_token"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ueber_tim_fragen(text):
    """Fragt Tims Chat statt Ollama direkt.

    Dadurch kann der gesprochene Weg alles, was der getippte kann -
    heute Websuche und Seitenabruf, morgen mehr. Ohne das muesste hier
    eine zweite Fassung derselben Logik mitgeschleppt werden, die
    unweigerlich hinterherhinkt.
    """
    nachrichten = []
    for frage, antwort in verlauf[-MAX_VERLAUF:]:
        nachrichten.append({"role": "user", "content": frage})
        nachrichten.append({"role": "assistant", "content": antwort})
    nachrichten.append({"role": "user", "content": text})

    resp = requests.post(
        ZENTRALE_URL + "/api/chat",
        json={"modell": OLLAMA_MODEL, "nachrichten": nachrichten,
              "stil": "sprache"},
        headers={"X-M1-Token": _token_lesen()},
        timeout=300)
    daten = resp.json()
    if daten.get("fehler"):
        raise RuntimeError(daten["fehler"])
    antwort = _denken_entfernen(daten.get("antwort", ""))
    if daten.get("werkzeuge"):
        print(f"  (Tim hat nachgesehen: {', '.join(daten['werkzeuge'])})")
    return antwort


def _direkt_fragen(text):
    """Rueckfallebene: Ollama unmittelbar, ohne Werkzeuge.

    Nur fuer den Fall, dass die Zentrale gerade nicht laeuft - dann
    antwortet Tim wenigstens noch, statt zu schweigen.
    """
    kontext = ""
    for frage, antwort in verlauf[-MAX_VERLAUF:]:
        kontext += f"Frage: {frage}\nAntwort: {antwort}\n\n"
    prompt = f"{kontext}Frage: {text}\nAntwort:" if kontext else text
    # think=False statt "low": die Stufen (low/medium/high) sind
    # gpt-oss-Syntax, qwen3.5 kennt nur an/aus. Und am Sprachweg soll das
    # Modell nicht still gruebeln, sondern in den 300 Token antworten -
    # sonst frisst das Denken das Budget und Tim schweigt.
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
               "system": SPRECH_ANWEISUNG, "think": False,
               "keep_alive": "30m",
               "options": {"num_predict": 300, "num_ctx": MODELL_NUM_CTX}}
    resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
    return _denken_entfernen(resp.json().get("response", ""))


def modell_fragen(text):
    """Antwort auf eine gesprochene Frage holen."""
    try:
        antwort = ueber_tim_fragen(text)
    except Exception as fehler:
        print(f"Tims Chat nicht erreichbar ({fehler}) - frage Ollama direkt.")
        try:
            antwort = _direkt_fragen(text)
        except requests.exceptions.Timeout:
            return ("Das Modell braucht gerade zu lange - wahrscheinlich "
                    "laedt es noch. Frag mich gleich noch einmal.")
        except Exception as e:
            return f"Fehler: {e}"
    if not antwort:
        return "Das Modell hat keine Antwort geliefert."
    verlauf.append((text, antwort))
    return antwort


def _piper_nutzbar():
    """Piper nur nehmen, wenn es hier ueberhaupt laufen kann.

    Das mitgelieferte Piper ist eine x86_64-Binaerdatei und braucht auf
    Apple Silicon Rosetta. Zusaetzlich lag PIPER_BIN frueher auf einem
    Verzeichnis statt auf der Datei. Beides fuehrte zu "Permission denied"
    bei jeder Antwort - deshalb wird das hier vorher geprueft.
    """
    if not os.path.isfile(PIPER_BIN) or not os.access(PIPER_BIN, os.X_OK):
        return False
    if not os.path.isfile(PIPER_VOICE):
        return False
    try:
        probe = subprocess.run([PIPER_BIN, "--help"], capture_output=True, timeout=10)
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


PIPER_OK = _piper_nutzbar()
if not PIPER_OK:
    print(f"Sprachausgabe: macOS-Stimme '{MAC_STIMME}' (Piper nicht nutzbar)")


def sprechen(text):
    """Liest Text ueber das Standard-Ausgabegeraet vor.

    Bevorzugt die eingebauten macOS-Stimmen: nativ auf Apple Silicon, sechs
    deutsche Stimmen, keine Zusatzinstallation. Piper wird genutzt, wenn es
    lauffaehig ist - etwa nach dem Ablegen einer ARM-Fassung.
    """
    text = (text or "").strip()
    if not text:
        return

    if PIPER_OK:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        subprocess.run(
            [PIPER_BIN, "--model", PIPER_VOICE, "--output_file", wav_path],
            input=text.encode(), capture_output=True
        )
        subprocess.run(["afplay", wav_path])
        os.unlink(wav_path)
        return

    # macOS-Stimme. Laengere Antworten werden gekuerzt - ein Modell, das
    # zwei Absaetze schreibt, soll sie nicht komplett vorlesen.
    if len(text) > 700:
        text = text[:700] + " ... gekuerzt."
    try:
        subprocess.run(["say", "-v", MAC_STIMME, text], timeout=180)
    except (OSError, subprocess.SubprocessError) as fehler:
        print(f"Sprachausgabe fehlgeschlagen: {fehler}")


# --- Mikrofon-Pruefung -------------------------------------------------
# Der Mac Studio hat KEIN eingebautes Mikrofon. Ohne Eingabegeraet wirft
# sounddevice bei jedem Schleifendurchlauf "Error querying device -1" - eine
# Endlosschleife aus Fehlermeldungen, die nicht sagt, woran es liegt.
# Kabel schlaegt Bluetooth: AirPods & Co. schalten beim Mithoeren in den
# Telefonie-Modus (HFP) - Whisper versteht dann kaum noch etwas. Belegt
# am 21.08.2026: "startdüssel" statt "starte Odysseus", "was sehen drei
# sind" statt "was ist 10 mal 3000". Ein USB-/Webcam-Mikro liefert
# konstant gutes Audio, und die AirPods bleiben frei fuer Musik.
MIK_BEVORZUGT = ("web cam", "webcam", "usb")


def _mik_bevorzugt(eingaben):
    """Aus [(index, name), ...] das bevorzugte Geraet - oder None."""
    for index, name in eingaben:
        if any(muster in name.lower() for muster in MIK_BEVORZUGT):
            return index, name
    return None


def mikrofon_waehlen():
    """(Geraete-Index, Name) des besten Mikrofons - (None, None) ohne Mikro.

    Bevorzugt USB/Webcam, dann das Standardgeraet des Systems, dann das
    erste Eingabegeraet.
    """
    try:
        geraete = sd.query_devices()
    except Exception as fehler:
        print(f"Audio-Geraete nicht lesbar: {fehler}")
        return None, None
    eingaben = [(i, g["name"]) for i, g in enumerate(geraete)
                if g.get("max_input_channels", 0) > 0]
    if not eingaben:
        return None, None
    lieber = _mik_bevorzugt(eingaben)
    if lieber:
        return lieber
    try:
        std = sd.default.device[0]
        if std is not None and 0 <= std < len(geraete) \
                and geraete[std].get("max_input_channels", 0) > 0:
            return std, geraete[std]["name"]
    except Exception:
        pass
    return eingaben[0]


def _audio_neu_laden():
    """PortAudio neu initialisieren, damit neue Geraete auftauchen.

    sounddevice merkt sich die Geraeteliste vom Start. Eine spaeter
    angesteckte Webcam oder frisch verbundene AirPods erscheinen erst
    nach diesem Neuladen - ohne das wartete der Dienst ewig umsonst.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def auf_mikrofon_warten():
    """Blockiert, bis ein Eingabegeraet da ist. Nur fuer den Dienstmodus."""
    print("Kein Mikrofon gefunden - ich pruefe alle 15 Sekunden erneut.")
    while True:
        time.sleep(15)
        _audio_neu_laden()
        index, name = mikrofon_waehlen()
        if name:
            return index, name


_mik_index = None
_mikrofon = None
if "--selbsttest" not in sys.argv:
    _mik_index, _mikrofon = mikrofon_waehlen()
    if not _mikrofon and DIENST_MODUS:
        # Als Dienst nicht aufgeben: launchd wuerde den Prozess bei einem
        # Abbruch sowieso neu starten. Geduldig warten ist ruhiger.
        _mik_index, _mikrofon = auf_mikrofon_warten()
    elif not _mikrofon:
        print("")
        print("FEHLER: Kein Mikrofon gefunden - der Sprachassistent startet nicht.")
        print("")
        print("  Der Mac Studio hat kein eingebautes Mikrofon. Schliesse ein")
        print("  USB-Mikrofon oder Headset an (auch eine Webcam mit Mikrofon")
        print("  zaehlt) und waehle es unter")
        print("      Systemeinstellungen > Ton > Eingabe")
        print("  aus. Danach 'm1-talk' erneut starten.")
        print("")
        print("  Haengt eines dran und taucht trotzdem nicht auf, fehlt meist die")
        print("  Berechtigung:")
        print("      Systemeinstellungen > Datenschutz & Sicherheit > Mikrofon")
        print("")
        sys.exit(1)
    print(f"Mikrofon: {_mikrofon}")


# --- Sprachbefehle -----------------------------------------------------
# Feste Zuordnung von erkanntem Text zu einem erlaubten Befehl. Bewusst
# KEINE freie Shell und keine Modell-Entscheidung darueber, ob etwas
# ausgefuehrt wird - dieselbe Trennung wie in den Optional-Modulen, wo
# pruefe.sh deterministisch entscheidet und das Modell nur erklaeren darf.
# Trifft keine Regel, geht der Satz wie bisher als Frage an das Modell.
#
# Bewusst NICHT enthalten: das Aufheben des Kill-Switch. Etwas abschalten
# darf eine Fehlerkennung, wieder scharf schalten nicht.
DEPLOY_DIR = os.path.expanduser("~/Desktop/M1_DEPLOYMENT")
VENV_PY = "/opt/ki-server/venv/bin/python"
HARNESS_DIR = "/opt/ki-server/harness"
SPRACHBEFEHLE = [
    (("status", "gesundheit", "healthcheck", "wie geht es dir"),
     ["bash", DEPLOY_DIR + "/scripts/10_MAC_healthcheck.sh"],
     "Ich pruefe den Systemzustand."),
    (("modell scan", "modellscan", "neue modelle"),
     [VENV_PY, HARNESS_DIR + "/crew_generic.py", "modell_scan"],
     "Ich starte den Modell-Scan. Das dauert einen Moment."),
    (("welche ablaeufe", "welche abläufe", "was kannst du starten"),
     [VENV_PY, HARNESS_DIR + "/crew_generic.py"],
     "Ich hole die Liste der Ablaeufe."),
    (("autonomie", "was darfst du"),
     [VENV_PY, HARNESS_DIR + "/autonomie.py"],
     "Ich zeige die Autonomie-Lage."),
    (("selbsttest", "selbsttests", "teste dich"),
     ["bash", DEPLOY_DIR + "/scripts/14_MAC_selbsttests.sh"],
     "Ich fahre die Selbsttests."),
    # Der Scan steht VOR der Status-Regel: "scanne die umgebung" ist der
    # engere Wunsch. Stuende die Funkbruecken-Regel zuerst, bliebe jeder
    # Scan-Zuruf mit dem Wort "Funk" an ihrem "funk bruecke" haengen.
    # Allgemein: Wo ein Schluessel Praefix eines anderen ist, muss der
    # laengere/engere zuerst stehen.
    (("funk scan", "funkscan", "funk scannen", "umgebung scannen",
      "scanne die umgebung", "wer funkt", "lampen suchen",
      "such nach lampen", "ble scan", "bluetooth scan"),
     [VENV_PY, DEPLOY_DIR + "/hardware/pico_bruecke/bruecke_cli.py",
      "scannen", "4000"],
     "Ich hoere, wer in der Naehe funkt. Das dauert ein paar Sekunden."),
    (("funkbruecke", "funk bruecke", "funkbrücke", "funk brücke",
      "pico", "picow", "pico w"),
     [VENV_PY, DEPLOY_DIR + "/hardware/pico_bruecke/bruecke_cli.py", "status"],
     "Ich pruefe die Funkbruecke."),
    # Nur LESEN. Die Kamera selbst fasst allein kamera_dienst.py an - an
    # ihm haengt die macOS-Kameraerlaubnis. Hier kommt nur die gemessene
    # Farbe zurueck. Ein Rundruf an die Lampen steht bewusst NICHT in
    # dieser Liste; dieselbe Grenze zieht m1_job_server.py.
    (("kamera", "was siehst du", "welche farbe", "farbe messen",
      "farbmessung", "was fuer eine farbe", "was für eine farbe"),
     [VENV_PY, DEPLOY_DIR + "/hardware/kamera/kamera_cli.py", "messung"],
     "Ich schaue durch die Kamera."),
]


def stop_setzen():
    """Kill-Switch an allen Orten setzen, die autonomie.py prueft."""
    gesetzt = []
    for ordner in ("/opt/ki-server", os.path.expanduser("~/Desktop/M1_DEPLOYMENT")):
        try:
            os.makedirs(ordner, exist_ok=True)
            with open(os.path.join(ordner, "STOP"), "a"):
                pass
            gesetzt.append(ordner)
        except OSError:
            pass
    return gesetzt


# --- Licht: Raum und Wunsch aus dem Satz lesen -------------------------
# Anders als die uebrigen Regeln steht der Befehl hier nicht fest: Raum
# und Farbe kommen erst aus dem Gesagten. Die Raumnamen werden bei jedem
# Zuruf frisch aus lampen.json gelesen - benennt Mexla einen Raum um, gilt
# das sofort auch fuer die Sprache.
LAMPEN_LISTE = DEPLOY_DIR + "/hardware/pico_bruecke/lampen.json"

# Nur diese Farben, keine freie Eingabe. Was hier nicht steht, wird nicht
# gefunkt - so kann ein missverstandener Satz keine sinnlosen Pakete
# ausloesen.
# Zwei Listen, mit Absicht: Diese hier ist die Positivliste des
# SPRACHWEGS - was hier nicht steht, wird nie gefunkt, egal was
# Whisper verstanden hat. lampen_deutung.GRUNDFARBEN uebersetzt
# danach in RGB und gilt auch fuer Home Assistant. Beide muessen
# zusammenpassen; der Selbsttest unten prueft genau das.
LICHT_FARBEN = (
                "rot", "gruen", "blau", "gelb", "cyan",
                "magenta", "orange", "violett", "rosa", "tuerkis",
                "weiss", "lila", "pink", "purpur", "hellblau",
                "dunkelblau", "hellgruen", "dunkelgruen", "warmweiss", "kaltweiss",
                "gold", "bernstein", "mint", "flieder", "koralle",
                "limette", "indigo")

# Der Diskomodus ist der einzige Wunsch, den die Lampe selbst weiterlaeuft
# (Farbwechsel ohne weitere Funksprueche). Umgangssprachlich heisst er
# vieles - "disko", "party", "effekt", "farbwechsel" -, gemeint ist immer
# derselbe Befehl. lampen_steuern.py kennt davon nur "disko"/"effekt"/
# "party"; die uebrigen Schreibweisen werden hier auf "disko"
# zurueckgefuehrt, damit die Uebersetzung an einer Stelle steht.
# "disco" ist mit, weil Whisper das englische c oft mitschreibt.
LICHT_EFFEKT = ("disko", "diskomodus", "disco", "discomodus", "party",
                "partymodus", "effekt", "effektmodus", "farbwechsel")

# Wie schnell der Effekt die Farben wechselt. Die Lampe versteht das
# verkehrt herum - **kleiner ist schneller** -, deshalb stehen hier die
# fertigen Werte statt einer Rechnung: 2 ist das schnellste, 200 das
# langsamste, das nachgemessen etwas bewirkt (23.08.2026: 7 gegen 4
# Farbwechsel in zwoelf Sekunden).
LICHT_EFFEKT_SCHNELL = ("schnell", "schneller", "flott", "hektisch", "wild")
LICHT_EFFEKT_LANGSAM = ("langsam", "langsamer", "gemuetlich", "ruhig", "sanft")

# Alles, was hinter einem Raumnamen als eigenstaendiger Wunsch stehen
# darf - gebraucht fuer die Kompositum-Trennung in licht_aus_satz.
LICHT_WUNSCH_WOERTER = (frozenset(LICHT_EFFEKT) | frozenset(LICHT_FARBEN)
                        | {"an", "aus", "hell", "dunkel"})


def _licht_raeume():
    """Raumnamen aus der angelernten Liste (leer, wenn es sie nicht gibt)."""
    try:
        with open(LAMPEN_LISTE, encoding="utf-8") as f:
            return sorted((str(n).lower() for n in json.load(f).values()),
                          key=len, reverse=True)
    except Exception:
        return []


def _wort_drin(wort, satz):
    """Steht das Wort als eigenes Wort im Satz?

    Ohne Wortgrenzen waere "an" in "Kinderzimmer" enthalten und jeder
    Zuruf haette die Lampe eingeschaltet.
    """
    return re.search(r"(?<![a-z])" + re.escape(wort) + r"(?![a-z])", satz) is not None


# Ein Lampenbefehl ist eine AUFFORDERUNG, keine Beschreibung. Diese
# Tunwoerter unterscheiden beides: "mach das Licht im Flur aus" will
# etwas, "das Licht im Flur ist kaputt" erzaehlt nur davon.
LICHT_TUNWORT = ("mach", "mache", "machs", "schalte", "schalt", "stelle",
                 "stell", "dimme", "dimm", "drehe", "dreh", "setze", "setz",
                 "aktiviere", "deaktiviere")

# Umgekehrt verraten diese Woerter, dass jemand ueber Licht REDET statt
# es zu wollen. Ein Satz mit ihnen darf nie funken - auch dann nicht,
# wenn zufaellig ein Tunwort darin steht.
LICHT_ERZAEHLT = ("ist", "war", "sind", "waren", "wird", "wurde", "hat",
                  "hatte", "habe", "hab", "haben", "kaputt", "geworden",
                  "gewesen", "glaube", "denke", "gestern")

# So kurz ist ein Zuruf ohne Tunwort noch als Befehl lesbar
# ("Wohnzimmer rot", "Kueche auf 50 Prozent"). Alles Laengere ohne
# Tunwort ist erzaehlter Text.
LICHT_KURZ = 4

# Ein Prozentwert im Zuruf ("auf 60 prozent"). Steht hier, weil zwei
# Stellen ihn brauchen: die Helligkeitsregel unten und die
# "ist"-Ausnahme davor.
PROZENT_MUSTER = re.compile(r"(?<![0-9])(100|[1-9][0-9]?)\s*prozent")


def _raum_sprechbar(raum):
    """ASCII-Raumnamen fuers Vorlesen zurueck in Umlaute ("buero" -> "büro").

    Die Raumliste ist ASCII (Riegel der Zentrale laesst nur [A-Za-z0-9_.-]
    durch), gesprochen soll es aber nach Deutsch klingen.
    """
    for ersatz, umlaut in (("ae", "ä"), ("oe", "ö"), ("ue", "ü")):
        raum = raum.replace(ersatz, umlaut)
    return raum


def _kompositum_trennen(satz, raeume):
    """Zusammengeklebte Raum+Wunsch-Woerter wieder trennen.

    Whisper schreibt Zurufe gern als deutsches Kompositum: Am 23.08.2026
    wurde aus "Büro Party" das eine Wort "büroparty" - die Wortgrenzen-
    Suche fand darin weder Raum noch Wunsch, und der Zuruf fiel durch
    zum Modell (17 s, falsche Antwort).

    Getrennt wird NUR, wenn nach dem Raumnamen (und einem optionalen
    Fugen-n/-s wie in "kuechenparty") ein bekanntes Wunschwort uebrig
    bleibt. "kuechentisch" bleibt deshalb ein Wort - der gemessene
    Schutz der Wortgrenzen (22.08.2026: "mach den kuechentisch rot"
    darf nicht funken) haengt genau daran.
    """
    woerter = []
    for wort in satz.split():
        ersatz = wort
        for raum in raeume:
            if wort != raum and wort.startswith(raum):
                rest = wort[len(raum):]
                if rest[:1] in ("n", "s") and rest[1:] in LICHT_WUNSCH_WOERTER:
                    rest = rest[1:]
                if rest in LICHT_WUNSCH_WOERTER:
                    ersatz = raum + " " + rest
                    break
        woerter.append(ersatz)
    return " ".join(woerter)


def licht_aus_satz(satz, raeume=None):
    """Aus einem Satz einen Lampenbefehl bauen - oder None.

    Rueckgabe: (raum, befehl) oder None. Reine Rechnung, damit der
    Selbsttest sie ohne Mikrofon und ohne Lampen pruefen kann.

    Warum die zwei Huerden vor der eigentlichen Auswertung: Ein Raumname
    plus irgendein Wunschwort ist viel zu wenig. Gemessen am 22.08.2026
    loeste die erste Fassung bei 10 von 15 harmlosen Saetzen einen
    Funkspruch aus - "das Licht im Flur ist kaputt" schaltete den Flur
    EIN, "im Wohnzimmer ist es dunkel geworden" schaltete ihn AUS. Im
    Protokoll ist das nie passiert, aber nur, weil aus dem laufenden Film
    zufaellig nie Raumname und Wunschwort zusammen ankamen.

    Verlangt werden deshalb beide Seiten: eine Aufforderung muss erkennbar
    sein (Tunwort oder sehr kurzer Zuruf), und es darf nichts dabeistehen,
    was den Satz als Erzaehlung ausweist.
    """
    raeume = _licht_raeume() if raeume is None else raeume
    satz = _kompositum_trennen(satz, raeume)
    # Als ganzes Wort suchen, sonst faende "Kuechentisch" die "Kueche".
    raum = next((r for r in raeume if _wort_drin(r, satz)), None)
    if not raum:
        return None                       # ohne Raum kein Lampenbefehl

    # Erzaehlt jemand nur, wird nicht geschaltet. Eine enge Ausnahme
    # (23.08.2026): Whisper verhoerte "büro auf 60 prozent" als "büro
    # ist 60 prozent" - im kurzen Telegramm mit Prozentwert ist "ist"
    # ein verhoertes "auf", kein Erzaehlen. Alles andere sperrt weiter:
    # laengere Saetze ("die luftfeuchtigkeit im buero ist 60 prozent")
    # haelt das Telegramm-Fenster auf, die Fernseh-Saetze ("das buero
    # ist an") tragen keinen Prozentwert, und jedes weitere Erzaehlwort
    # ("war", "kaputt", ...) bleibt ein Veto.
    erzaehlt = [w for w in LICHT_ERZAEHLT if _wort_drin(w, satz)]
    prozent_telegramm = (erzaehlt == ["ist"]
                         and len(satz.split()) <= LICHT_KURZ
                         and PROZENT_MUSTER.search(satz) is not None)
    if erzaehlt and not prozent_telegramm:
        return None

    # Ohne erkennbare Aufforderung ebenfalls nicht.
    if not (any(_wort_drin(w, satz) for w in LICHT_TUNWORT)
            or len(satz.split()) <= LICHT_KURZ):
        return None

    # Ausschalten zuerst pruefen: "mach das Licht aus" enthaelt kein
    # "an", aber "anmachen" enthaelt "an" - waere "an" zuerst dran,
    # gaebe es bei "ausmachen" keinen Fehler, wohl aber umgekehrt.
    if any(_wort_drin(w, satz) for w in ("aus", "ausmachen", "ausschalten",
                                         "abschalten", "dunkel")):
        return raum, "aus"

    # Erst NACH dem Ausschalten: "mach die disko im flur aus" soll die
    # Lampe abschalten und nicht den Effekt neu starten. Vor den Farben,
    # weil "farbwechsel" sonst nur seine eigene Silbe waere - eine Farbe
    # steckt darin nicht (Wortgrenzen), aber die Reihenfolge sagt, was
    # gewinnt, wenn beides gesagt wird ("mach disko in rot").
    #
    # Wichtig: Diese Abfrage steht UNTER den beiden Huerden oben. Der
    # Diskomodus bekommt keinen eigenen, weicheren Weg - "im wohnzimmer
    # war gestern disko" scheitert wie jeder andere Lampensatz schon am
    # Erzaehlwort ("war", "gestern") und kommt hier gar nicht an.
    if any(_wort_drin(w, satz) for w in LICHT_EFFEKT):
        # "disko schnell" / "disko langsam" - das Tempo haengt als
        # zweites Feld am Befehl ("disko.2"). Der Punkt ist der Trenner,
        # den die Zentrale in Argumenten durchlaesst; lampen_steuern
        # deutet ihn.
        if any(_wort_drin(w, satz) for w in LICHT_EFFEKT_SCHNELL):
            return raum, "disko.2"
        if any(_wort_drin(w, satz) for w in LICHT_EFFEKT_LANGSAM):
            return raum, "disko.200"
        return raum, "disko"

    for farbe in LICHT_FARBEN:
        if _wort_drin(farbe, satz):
            return raum, farbe
    # Umlautschreibweisen, die Whisper liefert
    for gesprochen, gemeint in (("grün", "gruen"), ("weiß", "weiss"),
                                ("türkis", "tuerkis")):
        if _wort_drin(gesprochen, satz):
            return raum, gemeint

    treffer = PROZENT_MUSTER.search(satz)
    if treffer:
        return raum, "hell." + treffer.group(1)

    # "licht" steht hier bewusst NICHT mehr als Wunschwort: Es kommt in
    # jedem Satz ueber Lampen vor, auch im blossen "das Licht im Flur",
    # und sagte damit nichts darueber, ob jemand AN oder AUS will. Wer
    # einschalten will, sagt "an", "hell" oder "einschalten".
    # Das getrennte "schalte ... ein" wird eigens gesucht, weil "ein" als
    # einzelnes Wort viel zu haeufig ist ("ein Foto im Buero").
    if (any(_wort_drin(w, satz) for w in ("an", "anmachen", "einschalten",
                                          "anschalten", "hell"))
            or re.search(r"\bschalte?t?\b.*\bein\b", satz)):
        return raum, "an"
    return None


# Bereich je Programm - fuer die Anzeige im Chat, nicht fuer die Logik.
_BEREICH_JE_PROGRAMM = (
    ("bruecke_cli", "funk"),
    ("kamera_cli", "kamera"),
    ("crew_generic", "ablauf"),
    ("lampen_steuern", "licht"),
)


def _bereich_raten(befehl):
    """Grob einordnen, wofuer ein fester Befehl zustaendig ist."""
    ganz = " ".join(str(t) for t in befehl)
    for muster, bereich in _BEREICH_JE_PROGRAMM:
        if muster in ganz:
            return bereich
    return "system"


def _protokoll_melden(zuruf, antwort, weg, bereich):
    """Meldet einen Zuruf an die Zentrale, damit er im Chat auftaucht.

    IM HINTERGRUND und ohne jede Ruecksicht auf Erfolg. Der Grund steht
    im Kommentar der Gegenstelle: Der schnelle Weg schaltet das Licht in
    unter einer Sekunde, und genau das macht ihn im Alltag brauchbar.
    Wuerde hier auf eine HTTP-Antwort gewartet, kaeme Tims Ansage
    spaeter - die Meldung wuerde also genau die Eigenschaft kosten, die
    sie sichtbar machen soll. Faellt die Zentrale aus, ist das Licht
    trotzdem an; nur der Chateintrag fehlt.
    """
    def _senden():
        try:
            requests.post(
                ZENTRALE_URL + "/api/sprachprotokoll",
                json={"zuruf": zuruf, "antwort": antwort,
                      "weg": weg, "bereich": bereich},
                headers={"X-M1-Token": _token_lesen()},
                timeout=5)
        except Exception:
            pass
    threading.Thread(target=_senden, daemon=True).start()


def befehl_ausfuehren(text):
    """Deterministische Zuordnung Sprache -> Befehl.

    Gibt die zu sprechende Antwort zurueck, oder None, wenn keine Regel
    greift - dann uebernimmt wie bisher das Modell.
    """
    klein = _normalisieren(text.lower())

    # Not-Aus zuerst, damit er nie an einer anderen Regel haengen bleibt.
    if any(w in klein for w in ("notaus", "not aus", "kill switch", "stopp alles")):
        orte = stop_setzen()
        if orte:
            return "Kill-Switch gesetzt. Alle autonomen Ablaeufe sind gestoppt."
        return "Der Kill-Switch konnte nicht gesetzt werden. Bitte von Hand pruefen."

    # Licht vor der festen Tabelle: Ein Zuruf mit Raumnamen ist der
    # engere Wunsch. Ohne Raumnamen greift hier nichts, die Tabelle
    # bleibt also unberuehrt.
    licht = licht_aus_satz(klein)
    if licht:
        raum, wunsch = licht
        # Erst funken, dann sprechen (23.08.2026 nachgemessen): Die
        # Ansage VOR dem Funken dauerte 2,5 s, der Funkspruch selbst
        # 0,9 s - die Lampe schaltete also erst, wenn Tim ausgeredet
        # hatte, gefuehlte 4 s nach dem Zuruf. Jetzt schaltet sie zuerst;
        # die Bestaetigung nennt weiterhin den Raum, damit hoerbar
        # bleibt, was Tim verstanden hat.
        try:
            ergebnis = subprocess.run(
                [VENV_PY, DEPLOY_DIR + "/hardware/pico_bruecke/lampen_steuern.py",
                 raum] + wunsch.split("."),
                capture_output=True, text=True, timeout=120)
        except Exception as fehler:
            return f"Das Licht liess sich nicht schalten: {fehler}"
        ausgabe = ((ergebnis.stdout or "") + (ergebnis.stderr or "")).strip()
        if ausgabe.endswith("gesendet"):
            gesagt = "%s, erledigt." % _raum_sprechbar(raum).capitalize()
            _protokoll_melden(text, gesagt, "licht", "licht")
            return gesagt
        # URSACHE statt Symptom. "Das Licht hat nicht reagiert" klingt nach
        # einer kaputten Lampe - dabei WEISS das Werkzeug, woran es lag und
        # sagt es auch ("Bruecke nicht erreichbar"). Diese Auskunft
        # wegzuwerfen und durch eine allgemeine Klage zu ersetzen, schickt
        # Mexla an die falsche Stelle: Er sucht bei der Lampe, waehrend der
        # Pico am Schreibtisch liegt. (24.08.2026 real passiert - der Pico
        # war fuer einen Hardware-Test abgesteckt, und Tim sagte nur, das
        # Licht reagiere nicht.)
        if "nicht erreichbar" in ausgabe or "timed out" in ausgabe.lower():
            gesagt = ("Ich erreiche die Funkbruecke nicht. Der Pico haengt "
                      "vermutlich nicht am Strom oder nicht im WLAN - die "
                      "Lampen selbst sind wahrscheinlich in Ordnung.")
        elif "unbekannt" in ausgabe.lower():
            gesagt = "Den Raum kenne ich nicht."
        else:
            gesagt = "Das Licht hat nicht reagiert."
        _protokoll_melden(text, gesagt, "licht", "licht")
        return gesagt

    for schluessel, befehl, ansage in SPRACHBEFEHLE:
        if any(s in klein for s in schluessel):
            print(f"Befehl erkannt: {' '.join(befehl)}")
            sprechen(ansage)
            try:
                ergebnis = subprocess.run(befehl, capture_output=True,
                                          text=True, timeout=900)
            except subprocess.TimeoutExpired:
                return "Der Befehl hat zu lange gebraucht und wurde abgebrochen."
            except Exception as fehler:
                return f"Der Befehl ist fehlgeschlagen: {fehler}"
            ausgabe = (ergebnis.stdout or ergebnis.stderr or "").strip()
            letzte = [z.strip() for z in ausgabe.splitlines() if z.strip()][-6:]
            gesagt = ("Fertig. " + " ".join(letzte) if letzte
                      else "Fertig, ohne Ausgabe.")
            _protokoll_melden(text, gesagt, "befehl", _bereich_raten(befehl))
            return gesagt
    return None


# --- Selbsttest --------------------------------------------------------
# Laeuft ohne Mikrofon und ohne Modelle: geprueft wird, ob die Befehls-
# tabelle auf auffindbare Programme zeigt, ob Whisper samt Modellen da ist
# und ob die Kill-Switch-Orte mit autonomie.py uebereinstimmen.
# Steht bewusst VOR dem Selbsttest: nur so kann der den
# Fehlalarm-Abbruch am lebenden Ablauf pruefen (Attrappen
# statt Mikrofon, Whisper und Stimme).
def _verarbeite(puffer, text):
    """Weckwort erkannt - Befehl holen, ausfuehren, antworten.

    Der Befehl kommt zuerst aus dem Puffer: Was direkt nach dem Weckwort
    gesagt wurde, ist bereits aufgezeichnet. Nur wenn dort nichts steht,
    wird nachgefragt.
    """
    print(f"Wake Word erkannt: '{text}'")

    # 1) Kurz ausreden lassen. Wer "Hey Tim, starte den Selbsttest" in einem
    # Atemzug sagt, ist beim Erkennen des Weckworts noch mitten im Satz -
    # ohne diese Pause holt der Puffer nur den Anfang (22.08.2026 belegt:
    # "herr tim, wie viele" statt der ganzen Frage).
    time.sleep(NACHLAUF)

    # 2) Alles seit dem Weckwort genau nachhoeren (medium statt base).
    t0 = time.time()
    audio = puffer_letzte(puffer, 8.0)
    genau = transkribieren(audio, COMMAND_MODEL, mitschneiden=True,
                           stichworte=True)
    t1 = time.time()
    print(f"Nachgehoert ({t1-t0:.1f}s, {len(audio)/SAMPLE_RATE:.1f}s Audio, "
          f"RMS {float(np.sqrt(np.mean(audio**2))):.4f}): {genau}")

    # Auch im genauen Text wird das Weckwort klanglich gesucht - medium
    # verhoert sich ebenso ("herr tim, wie viele"). Alles danach ist der
    # Befehl.
    norm_genau = _normalisieren(genau.lower())
    stelle = weckwort_finden(norm_genau)

    # Das genaue Nachhoeren ist zugleich die Gegenprobe. Findet medium
    # kein Weckwort, hat sich base verhoert - dann still zurueck, ohne
    # "Ja?" und ohne Antwort.
    #
    # Warum das traegt (Protokoll 22.08.2026): Bei allen 6 echten
    # Weckrufen mit Nachhoeren stand das Weckwort auch im genauen Text
    # ("hey tim", "herr tim"), beim einzigen Fehlalarm nicht. Der Preis
    # eines Irrtums ist ungleich verteilt - ein verworfener echter Ruf
    # kostet ein zweites "Hey Tim", ein durchgelassener Fehlalarm laesst
    # Tim ungefragt in den Raum sprechen.
    if stelle is None:
        print(f"Fehlalarm verworfen (kein Weckwort im Nachhoeren): {genau}")
        return

    befehl = " ".join(norm_genau.split()[stelle[1]:]).strip()

    # 3) Steht dort schon ein brauchbarer Satz, sofort ausfuehren. Ein
    # einzelnes Wort reicht, wenn es bereits ein vollstaendiger
    # Lampenbefehl ist: Whisper klebt "Büro Party" gern zum Kompositum
    # "büroparty" zusammen (23.08.2026 im Protokoll), und die Nachfrage
    # "Ja?" kostete dann 6 s Nachhoeren, das meist ins Leere lief.
    if len(befehl.split()) >= 2 or licht_aus_satz(befehl) is not None:
        print(f"Befehl (aus dem Puffer): {befehl}")
    else:
        # 4) Sonst nachfragen und danach den Puffer nachhoeren. Vor dem
        # Warten wird geleert, damit Tims eigenes "Ja?" nicht mitzaehlt.
        sprechen("Ja?")
        puffer.clear()
        time.sleep(6.0)
        audio = puffer_letzte(puffer, 6.0)
        befehl = transkribieren(audio, COMMAND_MODEL, mitschneiden=True,
                                stichworte=True)
        print(f"Aufnahme: {len(audio)/SAMPLE_RATE:.1f}s, "
              f"RMS {float(np.sqrt(np.mean(audio**2))):.4f}, "
              f"Spitze {float(np.max(np.abs(audio))) if len(audio) else 0:.3f} "
              f"({MITSCHNITT})")
        print(f"Befehl: {befehl}")

    if not befehl.strip():
        sprechen("Ich habe nichts verstanden.")
        return

    t2 = time.time()
    antwort = befehl_ausfuehren(befehl)
    if antwort is None:
        print("Denke nach...")
        antwort = modell_fragen(befehl)
    print(f"Antwort ({time.time()-t2:.1f}s denken): {antwort}")
    sprechen(antwort)


if "--selbsttest" in sys.argv:
    _fehler = 0

    def _pruefe(bedingung, text, zusatz=""):
        global _fehler
        if bedingung:
            print(f"  ok      {text}")
        else:
            print(f"  FEHLER  {text}" + (f"  [{zusatz}]" if zusatz else ""))
            _fehler += 1

    print("sprachassistent Selbsttest:")

    # Jede Sprachregel muss auf ein auffindbares Programm zeigen - sonst
    # scheitert sie erst beim Zuruf, nicht hier.
    for _schluessel, _befehl, _ansage in SPRACHBEFEHLE:
        _programm = _befehl[0]
        _pruefe(shutil.which(_programm) is not None,
                f"Programm auffindbar: '{_schluessel[0]}'", _programm)

    _pruefe(shutil.which(WHISPER_BIN) is not None,
            "whisper auffindbar", str(WHISPER_BIN))
    _pruefe(os.path.isfile(WAKE_MODEL), "Weckwort-Modell vorhanden", WAKE_MODEL)
    _pruefe(os.path.isfile(COMMAND_MODEL), "Befehls-Modell vorhanden",
            COMMAND_MODEL)

    # Dieselbe Falle beim Funk: Die engere Scan-Regel muss vor der
    # Status-Regel stehen, sonst faengt "funk bruecke" jeden Scan ab.
    _scan = _funkstatus = None
    for _i, (_schluessel, _befehl, _ansage) in enumerate(SPRACHBEFEHLE):
        if "funk scan" in _schluessel:
            _scan = _i
        if "funkbruecke" in _schluessel:
            _funkstatus = _i
    _pruefe(_scan is not None and _funkstatus is not None and _scan < _funkstatus,
            "Funk: Scan-Regel steht vor der Status-Regel",
            f"scan={_scan}, status={_funkstatus}")

    # Die Grenze zur Lampensteuerung als Test, nicht als Kommentar:
    # Solange der Mesh-Schluessel fehlt, darf kein Zuruf einen Rundruf
    # absetzen. Wer "senden" hier eintraegt, sieht es sofort.
    _funkend = [_s[0] for _s, _b, _a in SPRACHBEFEHLE
                if any("bruecke_cli.py" in _t for _t in _b)
                and any(_t in ("senden", "stoppen") for _t in _b)]
    _pruefe(not _funkend,
            "kein Zuruf sendet Rundrufe (Lampensteuerung ist nicht scharf)",
            ", ".join(_funkend))

    # Lichtregel: ueber Licht REDEN darf nie Licht SCHALTEN.
    #
    # Die erste Fassung vom 22.08.2026 nahm jeden Raumnamen plus
    # irgendein Wunschwort. Gemessen loeste sie bei 10 von 15 harmlosen
    # Saetzen einen Funkspruch aus - "das licht im flur ist kaputt"
    # schaltete den Flur EIN. Diese Saetze sind der Beleg, dass es
    # aufhoert; ohne sie waere die Verschaerfung nur eine Behauptung.
    _raeume_probe = ["kinderzimmer", "schlafzimmer", "wohnzimmer",
                     "kinderbett", "esszimmer", "kueche", "buero", "flur"]
    for _harmlos in ("ich war heute im buero",
                     "das licht im flur ist kaputt",
                     "im wohnzimmer ist es dunkel geworden",
                     "das kinderzimmer ist hell genug",
                     "im schlafzimmer war das licht aus",
                     "das licht im flur",
                     "ich glaube im buero ist noch licht an",
                     "der kuechentisch ist rot",
                     "im wohnzimmer haben wir rot gestrichen",
                     "gestern im flur war alles dunkel",
                     "das buero ist zu hell",
                     "wir haben im esszimmer gegessen",
                     "die kueche muss ich noch aufraeumen",
                     "der flur", "buero",
                     # Der Raumname muss ein ganzes Wort sein: sonst
                     # steckt die "kueche" im "kuechentisch". Diese
                     # Saetze sind Aufforderungen ohne Erzaehlwort und
                     # kommen deshalb an beiden Huerden vorbei - nur die
                     # Wortgrenze haelt sie auf.
                     "mach den kuechentisch rot",
                     "stell die kuechenuhr auf gruen",
                     # Kurze Beschreibungen aus dem Fernseher passen ins
                     # Telegramm-Fenster und haetten ohne die
                     # Erzaehlwoerter gefunkt - nur "ist"/"war" haelt
                     # sie auf.
                     "das buero ist an", "flur ist dunkel",
                     "kueche ist hell", "wohnzimmer war aus",
                     "stell dir vor, im buero ist rot",
                     # Laengeres Gerede ohne Tunwort: hier haelt allein
                     # das enge Telegramm-Fenster (LICHT_KURZ) auf. Wer
                     # es aufbohrt, sieht es an diesen Zeilen.
                     "und dann im flur alles dunkel",
                     "drueben im wohnzimmer alles dunkel",
                     "irgendwo da hinten im buero ganz rot",
                     "nur noch die kueche an",
                     # Diskomodus (23.08.2026): Er darf sich keinen
                     # eigenen, weicheren Weg an den beiden Huerden vorbei
                     # bauen. Genau das waere die naheliegende Umsetzung
                     # gewesen ("steht 'disko' drin, dann funk") - diese
                     # Zeilen halten sie auf. "party" und "disko" fallen
                     # im Fernsehton oft, deshalb sind sie hier so
                     # ausfuehrlich vertreten wie die Farben oben.
                     "im wohnzimmer war gestern disko",
                     "die party gestern im buero war gut",
                     "der effekt ist kaputt",
                     "der farbwechsel im flur ist kaputt",
                     "im kinderzimmer haben wir party gemacht",
                     "gestern abend war im flur richtig disko",
                     "das wohnzimmer ist eine disko",
                     # Laenger als das Telegramm-Fenster, ohne Tunwort:
                     # hier haelt allein LICHT_KURZ auf.
                     "und dann im esszimmer die ganze nacht party",
                     # Wortgrenze auch fuer die Effektwoerter: dieser Satz
                     # kommt an BEIDEN Huerden vorbei (Tunwort "stell",
                     # kein Erzaehlwort) und wird allein davon gestoppt,
                     # dass "diskokugel" nicht das Wort "disko" ist.
                     "stell die diskokugel in die kueche",
                     "mach das kuechenradio lauter"):
        _pruefe(licht_aus_satz(_harmlos, _raeume_probe) is None,
                f"kein Lampenbefehl aus: '{_harmlos[:38]}'",
                repr(licht_aus_satz(_harmlos, _raeume_probe)))

    # Gegenprobe: Die Verschaerfung darf die Steuerung nicht abwuergen.
    # Ohne diese Haelfte waere ein totes licht_aus_satz() ein bestandener
    # Test.
    for _befehl, _erwartet in (("mach das buero an", ("buero", "an")),
                               ("mach das licht im flur aus", ("flur", "aus")),
                               ("wohnzimmer rot", ("wohnzimmer", "rot")),
                               ("kueche auf 50 prozent", ("kueche", "hell.50")),
                               ("schalte das esszimmer ein", ("esszimmer", "an")),
                               ("schlafzimmer blau", ("schlafzimmer", "blau")),
                               ("mach die kueche dunkel", ("kueche", "aus")),
                               ("dimm das buero auf 30 prozent", ("buero", "hell.30")),
                               ("flur aus", ("flur", "aus")),
                               ("mach das wohnzimmer hell", ("wohnzimmer", "an")),
                               ("stell das kinderbett auf gruen", ("kinderbett", "gruen")),
                               ("schalte das licht im flur aus", ("flur", "aus")),
                               # Diskomodus: alle Sprechweisen muessen auf
                               # denselben Befehl zeigen, sonst faellt eine
                               # davon still unter den Tisch.
                               ("mach disko im wohnzimmer", ("wohnzimmer", "disko")),
                               ("party im kinderzimmer", ("kinderzimmer", "disko")),
                               ("diskomodus flur", ("flur", "disko")),
                               ("farbwechsel in der kueche", ("kueche", "disko")),
                               ("mach partymodus im buero", ("buero", "disko")),
                               ("effekt im esszimmer", ("esszimmer", "disko")),
                               ("mach disco im schlafzimmer", ("schlafzimmer", "disko")),
                               # "aus" schlaegt den Effekt: wer die Disko
                               # beenden will, will nicht, dass sie wieder
                               # losgeht.
                               ("mach die disko im flur aus", ("flur", "aus"))):
        _tat = licht_aus_satz(_befehl, _raeume_probe)
        _pruefe(_tat == _erwartet,
                f"Lampenbefehl bleibt: '{_befehl[:38]}'",
                f"{_tat} statt {_erwartet}")

    # Umlaute (23.08.2026): Whisper schreibt "büro", die Raumliste kennt
    # nur "buero" - "büro rot" fiel deshalb durch die Lichtregel in den
    # Chat, und Tim schwieg 7 Minuten (300 s Chat-Timeout plus 120 s
    # Ollama-Rueckfall, belegt im Sprachprotokoll). Geprueft wird der
    # echte Weg: erst _normalisieren, dann die Regel - genau so baut
    # _verarbeite den Befehl.
    for _gesprochen, _erwartet in (
            ("büro rot", ("buero", "rot")),
            ("mach die küche grün", ("kueche", "gruen")),
            ("büro auf 40 prozent", ("buero", "hell.40")),
            ("schalte das büro ein", ("buero", "an"))):
        _tat = licht_aus_satz(_normalisieren(_gesprochen), _raeume_probe)
        _pruefe(_tat == _erwartet,
                f"Umlaut-Zuruf schaltet: '{_gesprochen}'",
                f"{_tat} statt {_erwartet}")

    # Die Gegenprobe dazu: erzaehlte Saetze mit Umlauten duerfen weiter
    # NICHT funken - die Vereinheitlichung darf die beiden Huerden
    # (Tunwort, Erzaehlwort) nicht aufweichen.
    for _harmlos in ("das licht im büro ist kaputt",
                     "die küche ist grün gestrichen",
                     "ich war heute im büro"):
        _tat = licht_aus_satz(_normalisieren(_harmlos), _raeume_probe)
        _pruefe(_tat is None,
                f"kein Lampenbefehl aus Umlaut-Satz: '{_harmlos[:38]}'",
                repr(_tat))

    # Disko-Tempo (24.08.2026): "kleiner ist schneller" ist die Falle -
    # wer die Zahlen vertauscht, baut einen Regler, der rueckwaerts
    # laeuft. Deshalb hier festgenagelt, nicht nur "es kommt etwas".
    for _gesprochen, _erwartet in (
            ("mach im buero disko schnell", ("buero", "disko.2")),
            ("mach im buero disko langsam", ("buero", "disko.200")),
            ("mach im buero disko", ("buero", "disko")),
            ("mach in der kueche party schneller", ("kueche", "disko.2")),
            ("mach in der kueche party gemuetlich", ("kueche", "disko.200"))):
        _tat = licht_aus_satz(_normalisieren(_gesprochen), _raeume_probe)
        _pruefe(_tat == _erwartet,
                f"Disko-Tempo: '{_gesprochen}'",
                f"{_tat} statt {_erwartet}")
    _pruefe(int(licht_aus_satz(_normalisieren("mach im buero disko schnell"),
                               _raeume_probe)[1].split(".")[1])
            < int(licht_aus_satz(_normalisieren("mach im buero disko langsam"),
                                 _raeume_probe)[1].split(".")[1]),
            "schnell ergibt die KLEINERE Zahl (die Lampe rechnet verkehrt herum)")

    # Komposita (23.08.2026): Whisper klebte "Büro Party" zum einen Wort
    # "büroparty" zusammen - die Wortgrenzen-Suche fand darin nichts,
    # und der Zuruf fiel zum Modell durch (17 s, falsche Antwort).
    # Getrennt wird nur Raum+Wunschwort (mit Fugen-n/-s), gemessen am
    # echten Weg ueber _normalisieren.
    for _gesprochen, _erwartet in (
            ("büroparty", ("buero", "disko")),
            ("küchenparty", ("kueche", "disko")),
            ("wohnzimmerrot", ("wohnzimmer", "rot"))):
        _tat = licht_aus_satz(_normalisieren(_gesprochen), _raeume_probe)
        _pruefe(_tat == _erwartet,
                f"Kompositum-Zuruf schaltet: '{_gesprochen}'",
                f"{_tat} statt {_erwartet}")

    # Die Gegenprobe: Die Trennung darf weder Erzaehlsaetze scharf
    # machen noch den Kuechentisch-Schutz beruehren ("mach den
    # kuechentisch rot" steht schon oben in der harmlosen Liste und
    # laeuft seit der Trennung ueber denselben Weg).
    for _harmlos in ("die büroparty gestern war gut",
                     "die küchenparty war laut"):
        _tat = licht_aus_satz(_normalisieren(_harmlos), _raeume_probe)
        _pruefe(_tat is None,
                f"kein Lampenbefehl aus Kompositum-Satz: '{_harmlos[:38]}'",
                repr(_tat))

    # Prozent (23.08.2026): Whisper schreibt "60%" als Zeichen statt
    # als Wort und verhoert "auf" als "ist" - "büro ist 60%" lief
    # deshalb ins Modell (49,7 s Ratlosigkeit) statt in die Regel.
    for _gesprochen, _erwartet in (
            ("büro auf 60%", ("buero", "hell.60")),
            ("büro ist 60%", ("buero", "hell.60")),
            ("büro ist 60 prozent", ("buero", "hell.60")),
            ("küche auf 5 %", ("kueche", "hell.5"))):
        _tat = licht_aus_satz(_normalisieren(_gesprochen), _raeume_probe)
        _pruefe(_tat == _erwartet,
                f"Prozent-Zuruf schaltet: '{_gesprochen}'",
                f"{_tat} statt {_erwartet}")

    # Die Gegenprobe: Die "ist"-Ausnahme gilt NUR im kurzen Telegramm
    # mit Prozentwert - erzaehlte Prozent-Saetze und die Fernseh-Saetze
    # bleiben stumm.
    for _harmlos in ("die luftfeuchtigkeit im büro ist 60 prozent",
                     "das büro war auf 60 prozent",
                     "ich glaube das büro ist auf 60 prozent",
                     "das büro ist an"):
        _tat = licht_aus_satz(_normalisieren(_harmlos), _raeume_probe)
        _pruefe(_tat is None,
                f"kein Lampenbefehl aus Prozent-Satz: '{_harmlos[:38]}'",
                repr(_tat))

    # Die Bestaetigung nennt den Raum wieder mit Umlaut - "buero"
    # vorgelesen klingt sonst nach Tippfehler.
    for _ascii, _gesprochen_form in (("buero", "büro"), ("kueche", "küche"),
                                     ("flur", "flur")):
        _pruefe(_raum_sprechbar(_ascii) == _gesprochen_form,
                f"Raumname sprechbar: {_ascii} -> {_gesprochen_form}",
                _raum_sprechbar(_ascii))

    # Seit _normalisieren Umlaute vereinheitlicht, erreichen Zurufe die
    # Befehlstabelle nur noch in ASCII-Schreibweise. Eine Umlaut-Variante
    # dort ist damit wirkungslos - sie darf aber nie die EINZIGE
    # Schreibweise sein, sonst faellt der Zuruf still durch zum Modell.
    for _schluessel, _befehl, _ansage in SPRACHBEFEHLE:
        for _wort in _schluessel:
            _pruefe(_normalisieren(_wort) in _schluessel,
                    f"Zuruf hat ASCII-Zwilling: '{_wort}'",
                    f"'{_normalisieren(_wort)}' fehlt in derselben Regel")

    # Zuordnungsprobe: Diese Saetze muessen bei der richtigen Regel
    # landen. Eine falsche Reihenfolge faellt hier auf, nicht erst beim
    # Zuruf in der Wohnung.
    def _welche_regel(_satz):
        _klein = _normalisieren(_satz.lower())
        for _s, _b, _a in SPRACHBEFEHLE:
            if any(_x in _klein for _x in _s):
                return _b
        return None

    for _satz, _erwartet in (("scanne die umgebung", "scannen"),
                             ("wer funkt hier", "scannen"),
                             ("pruef mal die funkbruecke", "status"),
                             ("welche farbe siehst du", "kamera_cli.py"),
                             ("was siehst du", "kamera_cli.py"),
                             ("starte den modell scan", "modell_scan")):
        _regel = _welche_regel(_satz)
        _pruefe(_regel is not None and any(_erwartet in _t for _t in _regel),
                f"Zuruf landet richtig: '{_satz}'",
                " ".join(_regel) if _regel else "keine Regel")

    # Genau der Fall vom 21.08.2026 abends: Whisper schrieb "hey, tim."
    # mit Komma, und der Zuruf fiel durch. Der Vergleich muss
    # Satzzeichen ueberleben.
    for _gehoert in ("hey, tim.", "hey tim!", "hey - tim"):
        _pruefe(any(ww in _normalisieren(_gehoert) for ww in WAKE_WORDS),
                f"Weckwort trotz Satzzeichen erkannt: '{_gehoert}'")

    # Direktbefehl im selben Atemzug: aus "hey, tim! starte den modell scan."
    # muss sauber "starte den modell scan" herausgeloest werden.
    _norm = _normalisieren("hey, tim! starte den modell scan.")
    _s = weckwort_finden(_norm)
    _pruefe(_s is not None
            and " ".join(_norm.split()[_s[1]:]) == "starte den modell scan",
            "Direktbefehl wird aus dem Weckwort-Satz herausgeloest",
            repr(_norm))

    # Klangliche Erkennung: So hat Whisper "Hey Tim" wirklich
    # geschrieben (Protokoll 21./22.08.2026). Eine feste Liste holt das
    # nie ein - diese Faelle MUESSEN ueber die Aehnlichkeit greifen.
    for _verhoert in ("peritim", "heritium", "helik tim", "herr tim",
                      "heitem", "peritim wie viel ist zehn mal drei tausend",
                      "hey tim", "hey, tim."):
        _pruefe(weckwort_finden(_normalisieren(_verhoert)) is not None,
                f"Weckwort klanglich erkannt: '{_verhoert[:34]}'")

    # Und die Gegenprobe: Whispers Rausch-Halluzinationen aus demselben
    # Protokoll duerfen Tim NICHT wecken.
    for _rauschen in ("musik", "stimmengewirr", "klingel", "schreit",
                      "türteln", "klacken", "tremper", "klatschen",
                      "ich bin ein bisschen aufgehört", "tschüss",
                      "der ist jetzt nicht so gut", "krachen",
                      "wie viel ist 10 mal 3000",
                      "ablauf ablauf healthcheck"):
        _pruefe(weckwort_finden(_normalisieren(_rauschen)) is None,
                f"kein Fehlalarm bei: '{_rauschen[:34]}'")

    # Der Fehlalarm vom 22.08.2026 abends: "nicht im" wird zu "nichtim",
    # teilt sich mit "heytim" das "h" und das "tim" und kam so auf 0.615.
    # Tim fragte "Ja?" und beantwortete einen Satz, der nie an ihn ging.
    # Entscheidend ist nicht die Hoehe der Aehnlichkeit, sondern wo sie
    # sitzt: hinten statt vorne.
    for _spaet in ("ich bin schneidergang und dann hat es mir nicht im leben.",
                   "nicht im", "das gilt nicht im august", "sicht im nebel"):
        _pruefe(weckwort_finden(_normalisieren(_spaet)) is None,
                f"kein Fehlalarm bei Endsilbe: '{_spaet[:34]}'",
                repr(_spaet))

    # Gegenprobe zur Ankerregel: Sie darf nur das Ende verwerfen, nicht
    # das Weckwort selbst. Genau diese Schreibweisen hat Whisper wirklich
    # geliefert - sie muessen den Anker passieren.
    for _vorne in ("peritim", "heritium", "helik tim", "heitem", "heritem",
                   "halt him", "hey tim"):
        _pruefe(_klang_am_anfang(_normalisieren(_vorne).replace(" ", "")),
                f"Weckwort faengt vorne an: '{_vorne}'")
    _pruefe(not _klang_am_anfang("nichtim"),
            "Endsilben-Treffer faellt am Anker durch")

    # Die Gegenprobe beim Nachhoeren (medium) - Datengrundlage des
    # Fehlalarm-Vetos in _verarbeite(): Bei echten Weckrufen stand das
    # Weckwort im genauen Text, beim Fehlalarm nicht.
    for _genau in ("hey tim, wie viel ist 10 mal 3000?",
                   "hey tim, was ist 10 mal 10?", "herr tim, wie viele"):
        _pruefe(weckwort_finden(_normalisieren(_genau)) is not None,
                f"Nachhoeren bestaetigt echten Ruf: '{_genau[:34]}'")
    _pruefe(weckwort_finden(_normalisieren(
                "ich bin schnell gegangen und dann hat man sich in die "
                "hose gemacht.")) is None,
            "Nachhoeren enttarnt den Fehlalarm (Veto greift)")

    # Der Fehlalarm vom 22.08.2026 gegen 23:50 - Tim sprach ungefragt in
    # den Raum. Aus dem laufenden Film kam "hey, ich bin hinterher
    # gefahren bei dem machtball"; "hey ich" -> "heyich" erreichte 0.667
    # und passierte auch die Gegenprobe beim Nachhoeren, weil dort
    # wieder ein "hey" stand. Das blosse "hey" reicht seitdem nicht mehr:
    # der NAME muss mitkommen. Alle vier Saetze stehen so im Protokoll.
    for _ohne_namen in ("hey, ich bin hinterher gefahren bei dem machtball.",
                        "hey, ich bin hinterher.",
                        "hey, die sampen sind jetzt echt gross.",
                        "hey, ich bin das denn?",
                        "ich mache mal so, hey, die lampen sind jetzt echt "
                        "gross und jetzt sind sie klein. was?",
                        "hey, kilo.", "hey, jetzt schwindelst du.",
                        "hey, du da drueben", "hey, was soll das"):
        _pruefe(weckwort_finden(_normalisieren(_ohne_namen)) is None,
                f"kein Fehlalarm ohne Namen: '{_ohne_namen[:34]}'",
                repr(weckwort_finden(_normalisieren(_ohne_namen))))

    # Gegenprobe zur Namensregel: Sie darf nur das nackte "hey"
    # verwerfen, nicht die verhoerten Weckworte. Genau diese
    # Schreibweisen hat Whisper wirklich geliefert.
    for _mit_namen in ("peritim", "heritium", "helik tim", "herr tim",
                       "heitem", "halt him", "haeltim", "hey tim",
                       "hei tim", "heli tim", "her tim", "hallo tim",
                       "heritem wie viel ist", "helik tim, v-sport,",
                       "halt him, was ist 10 mal?"):
        _pruefe(weckwort_finden(_normalisieren(_mit_namen)) is not None,
                f"Weckwort mit Namen bleibt: '{_mit_namen[:34]}'")
    _pruefe(_name_gedeckt("heytim") and not _name_gedeckt("heyich"),
            "Namensregel trennt 'heytim' von 'heyich'")

    # Und dasselbe am lebenden Ablauf: Mikrofon, Whisper und Stimme
    # werden durch Attrappen ersetzt, dann laeuft _verarbeite() wirklich.
    # Ohne diesen Test war der Abbruch ungedeckt - ihn auszubauen fiel
    # im Mutationstest am 22.08.2026 durch KEINE Pruefung auf.
    def _ablauf_mit(gehoert, ausfuehren=lambda t: "vier"):
        """Laesst _verarbeite() mit erfundenem Nachhoeren laufen.

        Rueckgabe: Liste dessen, was Tim gesagt haette.
        """
        gesagt = []
        echt = (puffer_letzte, transkribieren, sprechen,
                befehl_ausfuehren, time.sleep)
        globals()["puffer_letzte"] = \
            lambda *a, **k: np.zeros(SAMPLE_RATE, dtype="float32")
        globals()["transkribieren"] = lambda *a, **k: gehoert
        globals()["sprechen"] = lambda t: gesagt.append(t)
        globals()["befehl_ausfuehren"] = ausfuehren
        time.sleep = lambda s: None
        try:
            _verarbeite(collections.deque(), gehoert)
        finally:
            (globals()["puffer_letzte"], globals()["transkribieren"],
             globals()["sprechen"], globals()["befehl_ausfuehren"],
             time.sleep) = echt
        return gesagt

    _still = _ablauf_mit("ich bin schnell gegangen und dann hat man sich "
                         "in die hose gemacht.")
    _pruefe(_still == [], "Fehlalarm: Tim bleibt still (kein 'Ja?')",
            f"gesagt: {_still}")

    # Gegenrichtung - ein echter Ruf muss weiterhin durchkommen, sonst
    # waere der stille Assistent nur ein tauber.
    _echt_gesagt = _ablauf_mit("hey tim, wie viel ist 10 mal 3000?",
                               ausfuehren=lambda t: "dreissigtausend")
    _pruefe(_echt_gesagt == ["dreissigtausend"],
            "Echter Ruf kommt weiterhin durch", f"gesagt: {_echt_gesagt}")

    # Einwort-Kompositum (23.08.2026): "hey tim, büroparty." liess nur
    # das eine Wort "bueroparty" als Befehl uebrig - die Zwei-Woerter-
    # Regel fragte "Ja?" nach und hoerte 6 s in die Stille. Ist das eine
    # Wort schon ein vollstaendiger Lampenbefehl, muss es ohne Nachfrage
    # durchgehen.
    _eins_gesagt = _ablauf_mit("hey tim, büroparty.",
                               ausfuehren=lambda t: "Büro, erledigt.")
    _pruefe(_eins_gesagt == ["Büro, erledigt."],
            "Einwort-Lampenzuruf laeuft ohne Nachfrage",
            f"gesagt: {_eins_gesagt}")

    # Die Gegenprobe: Ein einzelnes Wort OHNE Lampensinn ("hey tim,
    # hallo.") muss weiterhin die Nachfrage ausloesen - sonst raten wir
    # bei jedem Brocken los.
    _brocken_gesagt = _ablauf_mit("hey tim, hallo.",
                                  ausfuehren=lambda t: "geraten")
    _pruefe(_brocken_gesagt[:1] == ["Ja?"],
            "Einzelwort ohne Lampensinn fragt weiter nach",
            f"gesagt: {_brocken_gesagt}")

    # Mikrofonwahl: Kabel/USB schlaegt Bluetooth (AirPods im
    # Telefonie-Modus lieferten "startdüssel" statt "starte Odysseus").
    _pruefe(_mik_bevorzugt([(0, "AirPods Pro Max"), (1, "WEB CAM")])
            == (1, "WEB CAM"),
            "USB-/Webcam-Mikrofon schlaegt Bluetooth")

    # Das Modell darf Ausfuehrungen nicht erfinden ("Alles klar, ich
    # starte jetzt" auf einen unverstandenen Befehl, 21.08.2026).
    _pruefe("Behaupte niemals" in SPRECH_ANWEISUNG,
            "Sprechanweisung verbietet erfundene Ausfuehrungen")

    # Ringpuffer (Siri-Prinzip): laueft der Strom durch, muss der Puffer
    # lueckenlos die juengste Vergangenheit liefern - sonst faellt ein
    # "Hey Tim" wieder zwischen zwei Fenster (22.08.2026: "peritum").
    _p = puffer_anlegen(sekunden=2.0)
    for _i in range(30):     # 30 Bloecke a 0,1s = 3s in einen 2s-Puffer
        _p.append(np.full(int(PUFFER_BLOCK * SAMPLE_RATE), float(_i),
                          dtype="float32"))
    _letzte = puffer_letzte(_p, 1.0)
    _pruefe(len(_letzte) == SAMPLE_RATE,
            "Ringpuffer liefert genau die angeforderte Laenge",
            f"{len(_letzte)} statt {SAMPLE_RATE}")
    # Der juengste Block muss am Ende stehen, der aelteste vorne raus.
    _pruefe(float(_letzte[-1]) == 29.0 and float(_letzte[0]) == 20.0,
            "Ringpuffer haelt die Reihenfolge und wirft Altes hinaus",
            f"vorne {_letzte[0]}, hinten {_letzte[-1]}")
    _pruefe(len(puffer_letzte(puffer_anlegen(), 3.0)) == 0,
            "Leerer Ringpuffer liefert leeres Audio")

    # Befehle werden am Stueck aufgenommen, nicht in Bloecken
    # (Blocktechnik lieferte am 22.08.2026 zerhacktes Audio).
    _pruefe(STILLE_ERKENNUNG is False,
            "Befehlsaufnahme laeuft am Stueck (Stille-Erkennung aus)")

    # Der Stichwortzettel darf nur beim Befehl mit, nicht beim Lauschen:
    # auf Rauschen antwortete Whisper sonst mit den Stichworten selbst
    # ("ablauf, ablauf, healthcheck", 22.08.2026).
    # Geprueft wird die Datei selbst: _verarbeite() steht weiter unten
    # und existiert zur Selbsttest-Zeit noch nicht.
    # Der Selbsttest-Block wird HERAUSGESCHNITTEN, bevor die Datei
    # durchsucht wird: Die Pruefzeilen unten enthalten die Suchtexte
    # selbst - wer die ganze Datei durchsucht, findet sich immer wieder
    # und besteht auch dann, wenn der echte Code die Stelle gar nicht
    # mehr hat. Am 22.08.2026 ist genau das im Harness zweimal passiert
    # und nur durch den Mutationstest aufgefallen. Nicht einfach "alles
    # vor dem Selbsttest" nehmen: die Hauptschleife steht dahinter.
    _ganz = open(__file__, encoding="utf-8").read()
    _vor, _, _rest = _ganz.partition('if "--selbsttest" in sys.argv:')
    _, _, _nach = _rest.partition("sys.exit(1 if _fehler else 0)")
    _datei = _vor + _nach
    import inspect as _inspect
    _quelle = _inspect.getsource(transkribieren)
    _pruefe("if stichworte:" in _quelle and '"--prompt"' in _quelle,
            "Stichwortzettel nur auf Anforderung")
    _pruefe(_datei.count("stichworte=True") >= 2,
            "Befehls-Transkription nutzt den Stichwortzettel",
            f"{_datei.count('stichworte=True')} Stellen")
    _pruefe("transkribieren(audio, WAKE_MODEL)" in _datei,
            "Weckwort-Lauschen laeuft ohne Stichwortzettel")
    _pruefe(NACHLAUF >= 2.0,
            "Nach dem Weckwort wird ausreden gelassen", f"{NACHLAUF}s")

    # Geraetewechsel im Betrieb: Der Strom muss regelmaessig erneuert und
    # das Mikrofon dabei neu gewaehlt werden. Ohne das lief Tim am
    # 22.08.2026 weiter auf der abgezogenen Webcam und "hoerte" Rauschen.
    _pruefe(0 < STROM_ERNEUERN <= 600,
            "Aufnahmestrom wird regelmaessig erneuert",
            f"{STROM_ERNEUERN}s")

    # Der gesprochene Weg muss ueber Tims Chat gehen, sonst kann er
    # dessen Werkzeuge nicht (Websuche, Seite lesen) und haengt bei
    # jeder Erweiterung hinterher.
    _pruefe('"stil": "sprache"' in _datei and "/api/chat" in _datei,
            "Sprache nutzt Tims Chat (und damit dessen Werkzeuge)")
    _pruefe(callable(_direkt_fragen) and callable(ueber_tim_fragen),
            "Rueckfall auf Ollama vorhanden, falls die Zentrale schweigt")
    _pruefe(_denken_entfernen("Mexla, es sind zehn Grad.") == "es sind zehn Grad.",
            "Ankerphrase wird nicht mitgesprochen")
    _pruefe(_denken_entfernen("<think>hm</think> Fertig.") == "Fertig.",
            "Denkschritte werden nicht mitgesprochen")
    _pruefe(_datei.count("mikrofon_waehlen()") >= 3,
            "Mikrofon wird bei jedem Stromaufbau neu bestimmt",
            f"{_datei.count('mikrofon_waehlen()')} Stellen")

    # Gleichlauf mit autonomie.py - wie im Selbsttest der Zentrale.
    try:
        if HARNESS_DIR not in sys.path:
            sys.path.insert(0, HARNESS_DIR)
        import autonomie
        _meine = {os.path.abspath(p) for p in STOP_ORTE}
        _seine = {os.path.abspath(str(p)) for p in autonomie.STOP_KANDIDATEN}
        _fehlend = _seine - _meine
        _pruefe(not _fehlend, "Kill-Switch-Orte decken autonomie.py ab",
                f"fehlen: {sorted(_fehlend)}")
    except ImportError as _e:
        _pruefe(False, "autonomie.py fuer den Gleichlauf ladbar", str(_e))

    if _fehler:
        print(f"\n{_fehler} Fehler.")
    sys.exit(1 if _fehler else 0)


# --- Bedienweg ohne Weckwort ------------------------------------------
# Dauerzuhoeren ist die fehleranfaelligste Stelle: ein kurzer Schnipsel,
# ein kleines Modell, Nebengeraeusche. Mit "--taste" faellt sie weg -
# Enter druecken, sprechen, fertig. Das nutzt direkt das genaue Modell.
if "--taste" in sys.argv:
    print("Tastenmodus: Enter druecken, dann sprechen (8 Sekunden).")
    print("Beenden mit Strg+C.\n")
    while True:
        try:
            input("[Enter zum Sprechen] ")
        except (EOFError, KeyboardInterrupt):
            print("\nBeendet.")
            break
        stop = killswitch_aktiv()
        if stop:
            print(f"Kill-Switch aktiv ({stop}) - beendet.")
            break
        print("Ich hoere...")
        audio = befehl_aufnehmen()
        gesagt = transkribieren(audio, COMMAND_MODEL)
        if not gesagt:
            print("Nichts verstanden.\n")
            continue
        print(f"Verstanden: {gesagt}")
        antwort = befehl_ausfuehren(gesagt)
        if antwort is None:
            print("Denke nach...")
            antwort = modell_fragen(gesagt)
        print(f"Antwort: {antwort}\n")
        sprechen(antwort)
    sys.exit(0)

print("Sprachassistent gestartet. Sage 'Hey Tim' um zu sprechen...")
print("Tipp: 'm1-talk --taste' startet den Modus ohne Weckwort (Enter druecken, sprechen).")
print("(Nutzt Mikrofon/Lautsprecher, die in den macOS-Toneinstellungen als Standard gesetzt sind)")

_stop_gemeldet = False   # Kill-Switch nur einmal melden, nicht alle 10 Sekunden
_fehler_folge = 0        # Fehler in Folge - ab 3 wird das Audio neu geladen


while True:
    # Aeussere Schleife: hier wird der Aufnahmestrom aufgebaut - und nach
    # einer Stoerung (Mikrofon abgezogen, Geraetewechsel) neu aufgebaut.
    try:
        stop = killswitch_aktiv()
        if stop:
            if DIENST_MODUS:
                # Nicht beenden: launchd startet den Dienst sonst sofort
                # neu. Stattdessen ruhen - der Strom ist hier zu, das
                # Mikrofon also wirklich aus.
                if not _stop_gemeldet:
                    print(f"Kill-Switch aktiv ({stop}) - ich ruhe, bis er aufgehoben wird.")
                    _stop_gemeldet = True
                time.sleep(10)
                continue
            print(f"Kill-Switch aktiv ({stop}) - Sprachassistent beendet.")
            break
        if _stop_gemeldet:
            print("Kill-Switch aufgehoben - ich hoere wieder zu.")
            _stop_gemeldet = False

        # Vor jedem Aufbau das Mikrofon neu bestimmen. Nach einem
        # Geraetewechsel stimmt die alte Geraetenummer nicht mehr: die
        # Nummern verschieben sich, wenn ein Geraet wegfaellt - der
        # Strom liefe dann auf dem falschen Eingang.
        _audio_neu_laden()
        _neuer_index, _neues_mik = mikrofon_waehlen()
        if not _neues_mik:
            if not DIENST_MODUS:
                print("Kein Mikrofon mehr da - beendet.")
                break
            _neuer_index, _neues_mik = auf_mikrofon_warten()
        if _neues_mik != _mikrofon:
            print(f"Mikrofon: {_neues_mik}")
        _mik_index, _mikrofon = _neuer_index, _neues_mik

        puffer = puffer_anlegen()

        def _rein(indata, frames, zeit, status):
            puffer.append(indata[:, 0].copy())

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32", device=_mik_index,
                            blocksize=int(PUFFER_BLOCK * SAMPLE_RATE),
                            callback=_rein):
            _fehler_folge = 0
            _strom_seit = time.time()
            while True:
                # Innere Schleife: der Strom laeuft durchgehend weiter,
                # hier wird nur nachgesehen, was inzwischen gesagt wurde.
                time.sleep(PRUEFTAKT)

                if killswitch_aktiv():
                    break          # Strom schliessen, oben weiterbehandeln

                # Regelmaessig erneuern, damit ein Geraetewechsel
                # auffaellt (siehe STROM_ERNEUERN).
                if time.time() - _strom_seit >= STROM_ERNEUERN:
                    break

                audio = puffer_letzte(puffer, PRUEFFENSTER)
                if len(audio) < int(1.0 * SAMPLE_RATE):
                    continue       # Puffer noch nicht gefuellt

                text = transkribieren(audio, WAKE_MODEL)
                if text.strip():
                    print(f"  gehoert: {text.strip()}")

                norm = _normalisieren(text)
                if weckwort_finden(norm):
                    _verarbeite(puffer, text)
                    # Nach der Antwort neu anfangen: sonst steckt das
                    # Weckwort noch im Puffer und loest gleich wieder aus.
                    puffer.clear()

    except KeyboardInterrupt:
        print("\nSprachassistent beendet.")
        break
    except Exception as e:
        print(f"Aufnahmestrom gestoert: {e}")
        _fehler_folge += 1
        if _fehler_folge >= 3:
            # Mehrfach hintereinander: meist ist das Eingabegeraet weg
            # (Webcam abgezogen, AirPods getrennt). Geraeteliste neu
            # laden und - im Dienstmodus - aufs naechste Mikrofon warten.
            _audio_neu_laden()
            _mik_index, _mikrofon = mikrofon_waehlen()
            if not _mikrofon and DIENST_MODUS:
                _mik_index, _mikrofon = auf_mikrofon_warten()
            print(f"Mikrofon: {_mikrofon}")
            _fehler_folge = 0
        time.sleep(2)
