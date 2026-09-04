#!/usr/bin/env python3
"""Die Projektschleife: Tim arbeitet an einer Werkstatt-Aufgabe, bis die
ABNAHME gruen ist - nicht bis er sagt, er sei fertig.

Auftrag Mexla, 04.09.2026: "baue die Schleife wie von dir beschrieben".

WORAUS SIE BESTEHT - und warum genau so:

1. Der Richter ist ein Abnahme-Skript ausserhalb von Tims Sandkasten
   (~/Desktop/Tim-Werkstatt/abnahmen/<aufgabe>.py, Exit 0 = fertig).
   Nie das Modell: laguna meldete am 03.09. "erledigt" bei
   unveraenderter Datei, gemma4 am 04.09. einen gruenen Test, der die
   Aufgabe nicht prueft. Ein Modell, das sich selbst abnimmt, nimmt
   sich immer ab.

2. FRISCHER KONTEXT JE RUNDE. Das Modell bekommt jede Runde nur:
   Aufgabe, Rundenzaehler, Stand der Datei, letzte Selbsttest- und
   Abnahme-Ausgabe. Keinen Gespraechsverlauf. Gemessen am 04.09.:
   gemma4 fiel bei 37 000 Token Kontext von 13 auf 5 Token/s und
   brauchte 22-28 Minuten je Aufruf; laguna verfiel am 03.09. bei
   wachsendem Verlauf in einen Wiederholungsloop (derselbe Absatz
   20-mal). Der Zustand liegt auf der Platte (Sandkasten, Protokoll),
   nicht im Fenster - das ist der Kern des "Ralph-Loop"-Musters.

3. BUDGET UND EHRLICHER AUSGANG. Hoechstens N Runden. Das Modell darf
   jede Runde mit STATUS: BLOCKED oder DECIDE beenden - dann haelt die
   Schleife an und legt Mexla die Frage vor. Ein "FERTIG" ohne gruene
   Abnahme ist kein Ende, sondern eine Rueckmeldung: "Die Abnahme sagt
   nein, und zwar deshalb."

Kein Abitur, keine Pruefungsflagge: Tim braucht hier Werkstatt und
(bei laguna) Shell. Vor dem Start wird geprueft, dass keine Pruefung
laeuft - eine Pruefung schaltet Tims Werkzeuge um.

Aufrufe:
  projektlauf.py <aufgabe> [--modell M] [--runden N] [--datei NAME]
  projektlauf.py --selbsttest
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOME = Path.home()
WERKSTATT = HOME / "Desktop" / "Tim-Werkstatt"
SANDKASTEN = WERKSTATT / "sandkasten"
ABNAHMEN = WERKSTATT / "abnahmen"
PROTOKOLLE = HOME / "Desktop" / "M1_DEPLOYMENT" / "docs"
VENV_PY = "/opt/ki-server/venv/bin/python"
HARNESS = Path("/opt/ki-server/harness")
ZENTRALE = "http://127.0.0.1:8770"
TOKEN_DATEI = HOME / ".m1_job_token"

MODELL_STANDARD = "laguna-xs-2.1"
RUNDEN_STANDARD = 10
RUNDEN_ZEITGRENZE_S = 3600          # Treiber-Geduld je Runde
AUSGABE_SELBSTTEST = 1500           # so viel der letzten Testausgabe sieht Tim
AUSGABE_ABNAHME = 1200
STATUS_WOERTER = ("WEITER", "FERTIG", "BLOCKED", "DECIDE")


def werkzeugbudget() -> int:
    """Wie viele Werkzeugaufrufe hat Tim je Runde? Aus der Zentrale, nicht geraten."""
    try:
        sys.path.insert(0, "/opt/ki-server/oberflaeche")
        from m1_zentrale import CHAT_WERKZEUGE_JE_RUNDE
        return int(CHAT_WERKZEUGE_JE_RUNDE)
    except Exception:                                       # noqa: BLE001
        return 8


# ----------------------------------------------------------------------
# Bausteine - alle einspeisbar, damit der Selbsttest ohne Zentrale,
# Ollama und Sandkasten auskommt (Tests fassen keine Betriebsdaten an).
# ----------------------------------------------------------------------

def aufgabe_lesen(name: str, laufen=None) -> str:
    """Den Aufgabentext holen - ueber werkstatt.py, nie am Sandkasten vorbei."""
    laufen = laufen or _werkstatt
    d = laufen("neu", name)
    if not d.get("ok"):
        raise RuntimeError("Aufgabe %r nicht lesbar: %s" % (name, d.get("fehler")))
    return d["aufgabe"]


def selbsttest_fahren(datei: str, laufen=None) -> dict:
    laufen = laufen or _werkstatt
    return laufen("testen", datei)


def abnahme_fahren(aufgabe: str, datei: str, laufen=None) -> dict:
    """Der Richter. Exit 0 = bestanden. Alles andere ist Stoff fuer die naechste Runde."""
    skript = ABNAHMEN / (aufgabe + ".py")
    ziel = SANDKASTEN / datei
    if laufen is None:
        def laufen(befehl):
            return subprocess.run(befehl, capture_output=True, text=True, timeout=300)
    if not skript.is_file():
        return {"bestanden": False, "ausgabe": "Kein Abnahme-Skript: %s" % skript,
                "umgebungsfehler": True}
    if not ziel.is_file():
        return {"bestanden": False,
                "ausgabe": "Die Datei %s liegt noch nicht im Sandkasten." % datei}
    try:
        lauf = laufen([VENV_PY, str(skript), str(ziel)])
    except Exception as f:                                  # noqa: BLE001
        return {"bestanden": False, "ausgabe": "Abnahme nicht fahrbar: %s: %s"
                % (type(f).__name__, f), "umgebungsfehler": True}
    aus = (lauf.stdout or "") + (lauf.stderr or "")
    return {"bestanden": lauf.returncode == 0, "ausgabe": aus}


def status_lesen(text: str) -> tuple:
    """Die letzte STATUS-Zeile des Modells: (wort, rest). Fehlt sie: ('WEITER', '')."""
    for zeile in reversed((text or "").strip().splitlines()):
        z = zeile.strip().lstrip("*# ").rstrip("* ")
        if z.upper().startswith("STATUS:"):
            rest = z.split(":", 1)[1].strip()
            wort = rest.split(":", 1)[0].split()[0].upper() if rest else "WEITER"
            wort = wort.rstrip(".,;")
            if wort in STATUS_WOERTER:
                grund = rest.split(":", 1)[1].strip() if ":" in rest else ""
                return wort, grund
    return "WEITER", ""


def rundennachricht(aufgabe_text: str, aufgabe: str, datei: str, runde: int,
                    runden: int, stand: dict, antwort: str = "") -> str:
    """Alles, was Tim in DIESER Runde wissen muss - und nichts von frueher.

    'antwort' ist Mexlas (oder Claudes) Antwort auf ein DECIDE/BLOCKED,
    mit dem der vorige Lauf endete - sie steht nur in Runde 1 des neuen
    Laufs ganz oben. So wird DECIDE zu einem echten Kanal in beide
    Richtungen statt zu einer Sackgasse.
    """
    teile = []
    if antwort and runde == 1:
        teile += ["ANTWORT AUF DEIN DECIDE/BLOCKED AUS DEM VORIGEN LAUF:",
                  antwort.strip(), ""]
    teile += [
        "PROJEKT %s - Runde %d von %d. Du arbeitest allein in deiner "
        "Werkstatt; Claude schaut nur zu, Mexla entscheidet am Ende."
        % (aufgabe, runde, runden),
        "",
        "DIE AUFGABE:",
        aufgabe_text.strip(),
        "",
        "STAND VOR DIESER RUNDE:",
    ]
    if stand.get("datei_da"):
        teile.append("- %s liegt im Sandkasten (%d Zeilen). Lies sie mit "
                     "aktion_starten werkstatt_lesen, bevor du schreibst - "
                     "das ist dein eigener Stand aus der vorigen Runde."
                     % (datei, stand.get("zeilen", 0)))
    else:
        teile.append("- %s gibt es noch nicht." % datei)
    if stand.get("dateien"):
        teile.append("- Alle Dateien in deinem Sandkasten: "
                     + "; ".join(stand["dateien"]))
    notizen = (stand.get("notizen") or "").strip()
    notizdatei = NOTIZEN_MUSTER % aufgabe
    if notizen:
        teile.append("")
        teile.append("DEINE NOTIZEN AUS DER VORIGEN RUNDE (%s):" % notizdatei)
        teile.append(notizen)
    else:
        teile.append("- Notizen von dir gibt es noch nicht (%s)." % notizdatei)
    st = stand.get("selbsttest")
    if st is not None:
        teile.append("- Dein Selbsttest (werkstatt_testen): %s"
                     % ("GRUEN" if st.get("ok") else "ROT (%s)" % st.get("phase", "?")))
        aus = str(st.get("ausgabe") or st.get("fehler") or "").strip()
        if aus:
            teile.append("  Ausgabe (Ende):\n  " + aus[-AUSGABE_SELBSTTEST:].replace("\n", "\n  "))
    ab = stand.get("abnahme")
    if ab is not None:
        teile.append("- Die ABNAHME (unabhaengig, entscheidet ueber fertig): %s"
                     % ("BESTANDEN" if ab.get("bestanden") else "NICHT BESTANDEN"))
        aus = str(ab.get("ausgabe") or "").strip()
        if aus:
            teile.append("  Ausgabe (Ende):\n  " + aus[-AUSGABE_ABNAHME:].replace("\n", "\n  "))
    teile += [
        "",
        "REGELN DIESER RUNDE:",
        "- Fertig ist, wenn die ABNAHME besteht - nicht, wenn du es sagst. "
        "Ein Selbsttest, der gruen ist, ohne die Aufgabe zu pruefen, "
        "zaehlt nicht.",
        "- Du hast %d Werkzeugaufrufe in dieser Runde. Sieben von sieben "
        "Runden bisher endeten mit aufgebrauchtem Budget mitten im Plan "
        "('Lassen Sie mich jetzt ...'). Deshalb: ERSTER Aufruf der Runde = "
        "werkstatt_schreiben %s mit deinem Plan (was du weisst, was du "
        "tust, naechster Schritt). DANN arbeiten: schreiben, testen, lesen "
        "was der Test sagt, nachbessern. Lies nicht dreimal dieselbe Datei."
        % (werkzeugbudget(), notizdatei),
        "- Behaupte nichts, was du nicht getan hast. Was du nicht geschafft "
        "hast, sagst du so - die naechste Runde kommt sowieso.",
        "- Die naechste Runde beginnt mit genau deinen Notizen aus %s - ohne "
        "sie faengst du wieder bei null an. Verstehen, was schon in den "
        "Notizen steht, kostet kein Budget mehr." % notizdatei,
        "- Beende deine Antwort mit GENAU EINER Zeile:",
        "  STATUS: WEITER          (du machst naechste Runde weiter)",
        "  STATUS: FERTIG          (du glaubst, die Abnahme besteht jetzt)",
        "  STATUS: BLOCKED: <warum> (du kommst ohne Hilfe nicht weiter)",
        "  STATUS: DECIDE: <frage>  (Mexla muss etwas entscheiden)",
        "- DECIDE ist NUR fuer Fragen, die die Aufgabe nicht beantwortet. "
        "'Soll ich weitermachen?' ist keine - die Aufgabe sagt ja. Das WIE "
        "entscheidest du, das OB die Abnahme.",
    ]
    return "\n".join(teile)


def eine_runde(chat, modell: str, chat_id: str, nachricht: str) -> dict:
    """Ein Chat-Aufruf. 'chat' ist einspeisbar (Test) - im Betrieb _chat_zentrale."""
    t0 = time.time()
    antwort = chat(modell, chat_id, nachricht)
    antwort = dict(antwort or {})
    antwort["dauer_s"] = round(time.time() - t0, 1)
    return antwort


def schleife(aufgabe: str, datei: str, modell: str = MODELL_STANDARD,
             runden: int = RUNDEN_STANDARD, chat=None, laufen=None,
             abnahme=None, melde=print, protokoll: Path = None,
             stand_datei=None, antwort: str = "") -> dict:
    """Die eigentliche Schleife. Liefert das Gesamtergebnis als dict."""
    chat = chat or _chat_zentrale
    abnahme = abnahme or abnahme_fahren
    stand_datei = stand_datei or _stand_datei
    aufgabe_text = aufgabe_lesen(aufgabe, laufen)
    chat_id = "projekt_%s_%s" % (aufgabe, datetime.now().strftime("%Y%m%d_%H%M"))
    ergebnis = {"aufgabe": aufgabe, "datei": datei, "modell": modell,
                "chat": chat_id, "begonnen": datetime.now().isoformat(timespec="seconds"),
                "runden": [], "ausgang": None}
    melde("=== PROJEKT %s | %s | bis zu %d Runden | Chat %s ===" % (aufgabe, modell, runden, chat_id))

    # Stand VOR Runde 1: Was liegt da, und was sagt der Richter?
    stand = stand_datei(datei, aufgabe)
    stand["abnahme"] = abnahme(aufgabe, datei)
    if stand["abnahme"].get("umgebungsfehler"):
        ergebnis["ausgang"] = "UMGEBUNGSFEHLER: " + stand["abnahme"]["ausgabe"]
        melde(ergebnis["ausgang"]); return ergebnis
    if stand["abnahme"]["bestanden"]:
        ergebnis["ausgang"] = "FERTIG (Abnahme bestand schon vor Runde 1)"
        melde(ergebnis["ausgang"]); return ergebnis
    if stand["datei_da"]:
        stand["selbsttest"] = selbsttest_fahren(datei, laufen)

    for k in range(1, runden + 1):
        nachricht = rundennachricht(aufgabe_text, aufgabe, datei, k, runden, stand, antwort)
        # 'erg', nicht 'antwort': Der Parameter 'antwort' ist Mexlas Antwort
        # auf ein DECIDE. Bis 04.09.2026 21:12 ueberschrieb der Rundenwert
        # denselben Namen - eine Gegenprobe flog mit "'dict' object has no
        # attribute 'strip'" auf, statt sauber rot zu werden. Latent, aber
        # echt: Ein Name, zwei Bedeutungen, ist ein Fehler auf Vorrat.
        erg = eine_runde(chat, modell, chat_id, nachricht)
        text = str(erg.get("antwort") or "")
        status, grund = status_lesen(text)
        # Nach der Runde: Stand neu erheben. Der Richter entscheidet.
        stand = stand_datei(datei, aufgabe)
        stand["selbsttest"] = selbsttest_fahren(datei, laufen) if stand["datei_da"] else None
        stand["abnahme"] = abnahme(aufgabe, datei)
        runde = {"runde": k, "dauer_s": erg.get("dauer_s"),
                 "werkzeuge": erg.get("werkzeuge") or [],
                 "fehler": erg.get("fehler"),
                 "status": status, "status_grund": grund,
                 "selbsttest_ok": bool((stand.get("selbsttest") or {}).get("ok")),
                 "abnahme_ok": bool(stand["abnahme"].get("bestanden")),
                 "text": text[:6000]}
        ergebnis["runden"].append(runde)
        melde("  Runde %d/%d: %s | Selbsttest %s | ABNAHME %s | %d Werkzeuge, %ss%s"
              % (k, runden, status + (": " + grund[:60] if grund else ""),
                 "gruen" if runde["selbsttest_ok"] else "rot",
                 "BESTANDEN" if runde["abnahme_ok"] else "nein",
                 len(runde["werkzeuge"]), runde["dauer_s"],
                 " | FEHLER: %s" % runde["fehler"] if runde["fehler"] else ""))
        if protokoll is not None:
            (protokoll / ("runde_%02d.json" % k)).write_text(
                json.dumps({"nachricht": nachricht, **runde,
                            "selbsttest": stand.get("selbsttest"),
                            "abnahme": stand.get("abnahme")},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        if stand["abnahme"].get("umgebungsfehler"):
            ergebnis["ausgang"] = "UMGEBUNGSFEHLER: " + stand["abnahme"]["ausgabe"]; break
        if stand["abnahme"]["bestanden"]:
            ergebnis["ausgang"] = "FERTIG nach %d Runde(n) - die Abnahme besteht" % k; break
        if status in ("BLOCKED", "DECIDE"):
            ergebnis["ausgang"] = "%s nach Runde %d: %s" % (status, k, grund or "(kein Grund genannt)"); break
        if erg.get("fehler") and k == runden:
            ergebnis["ausgang"] = "UMGEBUNGSFEHLER in letzter Runde: %s" % erg["fehler"]; break
    else:
        ergebnis["ausgang"] = "BUDGET ERSCHOEPFT nach %d Runden - Abnahme besteht nicht" % runden
    ergebnis["beendet"] = datetime.now().isoformat(timespec="seconds")
    melde("=== " + ergebnis["ausgang"] + " ===")
    return ergebnis


# ----------------------------------------------------------------------
# Die echten Helfer
# ----------------------------------------------------------------------

def _werkstatt(*args) -> dict:
    lauf = subprocess.run([VENV_PY, str(HARNESS / "werkstatt.py"), *args],
                          capture_output=True, text=True, timeout=300)
    try:
        return json.loads(lauf.stdout)
    except ValueError:
        return {"ok": False, "fehler": (lauf.stdout + lauf.stderr)[-500:]}


NOTIZEN_MUSTER = "NOTIZEN_%s.md"
NOTIZEN_GRENZE = 2500


def _stand_datei(datei: str, aufgabe: str = "") -> dict:
    """Was liegt im Sandkasten - und was hat Tim sich selbst notiert?

    Beobachtung aus dem ersten Lauf (04.09.2026, laguna, 5 Runden): Mit
    frischem Kontext fing jede Runde bei null an - Datei lesen, Test
    fahren, Klasse "genauer verstehen" - und das Budget war aufgebraucht,
    bevor es ans Schreiben ging. In Runde 4 lag der richtige rote Test
    schon als Nebendatei da; die Runden 5 ff. wussten es nicht. Frischer
    Kontext braucht ein Gedaechtnis AUF DER PLATTE: die Liste aller
    Dateien und Tims eigene Notizen (Plan, Entscheidung, naechster
    Schritt), die er am Rundenende schreibt und am Rundenanfang liest.
    """
    ziel = SANDKASTEN / datei
    stand = {"datei_da": ziel.is_file(), "zeilen": 0, "dateien": [], "notizen": ""}
    if stand["datei_da"]:
        try:
            stand["zeilen"] = ziel.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        except OSError:
            pass
    # Nur, was zu DIESER Aufgabe gehoert: Dateien mit dem Stamm der
    # Zieldatei und die Notizen. Der Sandkasten haelt auch die Uebungen
    # der Abiturlaeufe (auswertung.py, doppel.py, ...) - die haben hier
    # nichts verloren und wuerden nur ablenken. Ihre Zahl wird genannt,
    # damit nichts verschwiegen ist.
    stamm = Path(datei).stem
    fremde = 0
    try:
        for f in sorted(SANDKASTEN.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.suffix == ".pyc":
                continue
            if f.name.startswith(stamm) or (aufgabe and f.name == NOTIZEN_MUSTER % aufgabe):
                st = f.stat()
                stand["dateien"].append("%s (%d Zeilen, %s)" % (
                    f.name, f.read_text(encoding="utf-8", errors="replace").count("\n") + 1,
                    datetime.fromtimestamp(st.st_mtime).strftime("%H:%M")))
            else:
                fremde += 1
    except OSError:
        pass
    if fremde:
        stand["dateien"].append("(dazu %d Dateien anderer Uebungen, nicht diese Aufgabe)" % fremde)
    if aufgabe:
        n = SANDKASTEN / (NOTIZEN_MUSTER % aufgabe)
        if n.is_file():
            try:
                stand["notizen"] = n.read_text(encoding="utf-8", errors="replace")[-NOTIZEN_GRENZE:]
            except OSError:
                pass
    return stand


def _chat_zentrale(modell: str, chat_id: str, nachricht: str) -> dict:
    rumpf = json.dumps({"modell": modell, "chat": chat_id,
                        "nachrichten": [{"role": "user", "content": nachricht}]}).encode("utf-8")
    token = TOKEN_DATEI.read_text(encoding="utf-8").strip()
    a = urllib.request.Request(ZENTRALE + "/api/chat", data=rumpf, method="POST",
                               headers={"Content-Type": "application/json", "X-M1-Token": token})
    try:
        with urllib.request.urlopen(a, timeout=RUNDEN_ZEITGRENZE_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as f:
        return {"fehler": "%s: %s" % (type(f).__name__, f)}


def _pruefung_laeuft() -> bool:
    try:
        from pruefungsflagge import laeuft
        return laeuft() or Path("/opt/ki-server/config/PRUEFUNGSMODUS").exists()
    except Exception:                                       # noqa: BLE001
        return True


# ----------------------------------------------------------------------
# Selbsttest - hermetisch
# ----------------------------------------------------------------------

def selbsttest() -> int:
    fehler = []

    def pruefe(b, was, zusatz=""):
        if b:
            print("  ok      %s" % was)
        else:
            fehler.append(was); print("  FEHLER  %s  <- %s" % (was, zusatz))

    print("Selbsttest projektlauf\n")
    pruefe(status_lesen("bla\nSTATUS: FERTIG") == ("FERTIG", ""), "STATUS: FERTIG wird gelesen")
    pruefe(status_lesen("x\n**STATUS: BLOCKED: kein Zugriff auf die Datei**")
           == ("BLOCKED", "kein Zugriff auf die Datei"), "BLOCKED mit Grund, auch in Fettschrift")
    pruefe(status_lesen("STATUS: DECIDE: Schluessel aus Name+Kasten oder Liste?")[0] == "DECIDE",
           "DECIDE wird erkannt")
    pruefe(status_lesen("kein Status hier") == ("WEITER", ""), "ohne Statuszeile gilt WEITER")
    pruefe(status_lesen("STATUS: Erledigt") == ("WEITER", ""), "ein unbekanntes Wort ist kein Status")

    # Die Schleife mit Attrappen. Der Richter (abnahme) bestimmt das Ende.
    def laufen_stub(*a):
        if a[0] == "neu":
            return {"ok": True, "aufgabe": "Baue x."}
        return {"ok": False, "phase": "selbsttest", "ausgabe": "AssertionError: 1 statt 2"}
    protokoll_ids = []

    def mach_chat(antworten):
        def chat(modell, chat_id, nachricht):
            protokoll_ids.append((chat_id, nachricht))
            return {"antwort": antworten.pop(0), "werkzeuge": ["werkstatt_schreiben"]}
        return chat
    stand_da = lambda d, a="": {"datei_da": True, "zeilen": 42,
                                "dateien": ["x.py (42 Zeilen, 20:00)", "x_test.py (30 Zeilen, 20:05)"],
                                "notizen": "PLAN: Schluessel aus Name+Kasten. NAECHSTER SCHRITT: aufnehmen umbauen."}

    # 1) FERTIG ohne gruene Abnahme zaehlt nicht - es geht weiter.
    ab_immer_rot = lambda a, d: {"bestanden": False, "ausgabe": "(a) 1 statt 2"}
    e = schleife("x", "x.py", runden=3, chat=mach_chat(["STATUS: FERTIG", "STATUS: FERTIG", "STATUS: WEITER"]),
                 laufen=laufen_stub, abnahme=ab_immer_rot, melde=lambda *_: None, stand_datei=stand_da)
    pruefe(len(e["runden"]) == 3 and e["ausgang"].startswith("BUDGET"),
           "FERTIG ohne gruene Abnahme beendet NICHTS - Budget zaehlt", e["ausgang"])

    # 2) Gruene Abnahme beendet, auch wenn das Modell WEITER sagt.
    zaehler = {"n": 0}
    def ab_gruen_ab_2(a, d):
        zaehler["n"] += 1
        return {"bestanden": zaehler["n"] >= 3, "ausgabe": "ok" if zaehler["n"] >= 3 else "rot"}
    e = schleife("x", "x.py", runden=5, chat=mach_chat(["STATUS: WEITER"] * 5),
                 laufen=laufen_stub, abnahme=ab_gruen_ab_2, melde=lambda *_: None, stand_datei=stand_da)
    pruefe(e["ausgang"].startswith("FERTIG nach 2"), "gruene Abnahme beendet die Schleife - der Richter, nicht das Modell", e["ausgang"])

    # 3) BLOCKED haelt an und traegt den Grund.
    e = schleife("x", "x.py", runden=5, chat=mach_chat(["STATUS: BLOCKED: Datei unlesbar"] * 5),
                 laufen=laufen_stub, abnahme=ab_immer_rot, melde=lambda *_: None, stand_datei=stand_da)
    pruefe(len(e["runden"]) == 1 and e["ausgang"].startswith("BLOCKED nach Runde 1: Datei unlesbar"),
           "BLOCKED stoppt sofort und nennt den Grund", e["ausgang"])

    # 4) Frischer Kontext: jede Nachricht traegt die Aufgabe, keinen Verlauf.
    protokoll_ids.clear()
    # Die Regeln nennen die Statuswoerter selbst - deshalb traegt die
    # Attrappen-Antwort eine eigene Marke, an der man den Verlauf erkennt.
    schleife("x", "x.py", runden=2, chat=mach_chat(["MARKE_RUNDE_EINS erledigt.\nSTATUS: WEITER",
                                                    "MARKE_RUNDE_ZWEI\nSTATUS: WEITER"]),
             laufen=laufen_stub, abnahme=ab_immer_rot, melde=lambda *_: None, stand_datei=stand_da)
    n1, n2 = protokoll_ids[0][1], protokoll_ids[1][1]
    pruefe("Baue x." in n1 and "Baue x." in n2 and "Runde 2 von 2" in n2
           and "MARKE_RUNDE_EINS" not in n2,
           "jede Runde bekommt Aufgabe + Stand, nie die vorige Antwort")
    pruefe("1 statt 2" in n2, "die letzte Testausgabe steht in der naechsten Nachricht")
    pruefe(len({c for c, _ in protokoll_ids}) == 1, "ein Chat je Lauf (fuer Mexlas Mitlesen)")
    pruefe("x_test.py (30 Zeilen" in n2, "alle Sandkasten-Dateien stehen in der Nachricht - auch Nebendateien")
    pruefe("PLAN: Schluessel aus Name+Kasten" in n2 and "NOTIZEN_x.md" in n2,
           "Tims Notizen der vorigen Runde stehen in der naechsten Nachricht")
    pruefe("ERSTER Aufruf der Runde = werkstatt_schreiben NOTIZEN_x.md" in n2,
           "die Regel verlangt die Notizen als ERSTEN Aufruf der Runde")
    pruefe(("Du hast %d Werkzeugaufrufe" % werkzeugbudget()) in n2,
           "das Budget kommt aus der Zentrale, nicht aus einer geratenen Zahl")
    protokoll_ids.clear()
    schleife("x", "x.py", runden=2, chat=mach_chat(["A\nSTATUS: WEITER", "B\nSTATUS: WEITER"]),
             laufen=laufen_stub, abnahme=ab_immer_rot, melde=lambda *_: None, stand_datei=stand_da,
             antwort="Ja, weitermachen - das Wie ist deins.")
    a1, a2 = protokoll_ids[0][1], protokoll_ids[1][1]
    pruefe(a1.startswith("ANTWORT AUF DEIN DECIDE") and "das Wie ist deins" in a1,
           "eine Antwort auf ein DECIDE steht ganz oben in Runde 1")
    pruefe("das Wie ist deins" not in a2, "und nur dort - Runde 2 ist wieder frisch")
    pruefe("DECIDE ist NUR fuer Fragen" in a2, "die Regel grenzt DECIDE ein")

    # 5) Abnahme schon vor Runde 1 gruen -> keine Runde.
    e = schleife("x", "x.py", runden=3, chat=mach_chat(["STATUS: WEITER"]), laufen=laufen_stub,
                 abnahme=lambda a, d: {"bestanden": True, "ausgabe": "ok"}, melde=lambda *_: None, stand_datei=stand_da)
    pruefe(not e["runden"] and e["ausgang"].startswith("FERTIG (Abnahme bestand schon"),
           "was schon fertig ist, wird nicht bearbeitet")

    # 6) Fehlendes Abnahme-Skript ist Umgebung, kein Urteil.
    e = schleife("x", "x.py", runden=3, chat=mach_chat(["STATUS: WEITER"]), laufen=laufen_stub,
                 abnahme=lambda a, d: {"bestanden": False, "ausgabe": "Kein Abnahme-Skript", "umgebungsfehler": True},
                 melde=lambda *_: None, stand_datei=stand_da)
    pruefe(e["ausgang"].startswith("UMGEBUNGSFEHLER") and not e["runden"],
           "ohne Richter wird nicht gespielt")

    # Der echte Stand-Leser an einem Wegwerf-Sandkasten: nur Dateien der
    # Aufgabe werden gelistet, Fremdes nur gezaehlt, Notizen kommen mit.
    import tempfile
    global SANDKASTEN
    _echt_sk = SANDKASTEN
    with tempfile.TemporaryDirectory() as _o:
        SANDKASTEN = Path(_o)
        for name, inhalt in (("gedaechtnis.py", "a\nb\n"), ("gedaechtnis_test.py", "t\n"),
                             ("auswertung.py", "x\n"), ("doppel.py", "y\n"),
                             ("NOTIZEN_ball.md", "PLAN: so.\n")):
            (SANDKASTEN / name).write_text(inhalt, encoding="utf-8")
        try:
            st = _stand_datei("gedaechtnis.py", "ball")
        finally:
            SANDKASTEN = _echt_sk
    pruefe(st["datei_da"] and st["zeilen"] == 3, "Stand: Zieldatei erkannt, Zeilen gezaehlt", str(st))
    pruefe(any(d.startswith("gedaechtnis_test.py") for d in st["dateien"])
           and any(d.startswith("NOTIZEN_ball.md") for d in st["dateien"])
           and not any(d.startswith("auswertung") for d in st["dateien"]),
           "Stand: nur Dateien dieser Aufgabe werden gelistet", str(st["dateien"]))
    pruefe(any("2 Dateien anderer Uebungen" in d for d in st["dateien"]),
           "Stand: Fremdes wird gezaehlt, nicht verschwiegen")
    pruefe(st["notizen"].startswith("PLAN: so."), "Stand: Notizen werden gelesen")

    print("\n%s" % ("Alle Pruefungen bestanden." if not fehler else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


def main(argumente: list) -> int:
    if "--selbsttest" in argumente:
        return selbsttest()
    if not argumente or argumente[0].startswith("-"):
        print(__doc__); return 0
    aufgabe = argumente[0]
    modell = MODELL_STANDARD; runden = RUNDEN_STANDARD; datei = None; antwort = ""
    it = iter(argumente[1:])
    for a in it:
        if a == "--modell": modell = next(it)
        elif a == "--runden": runden = int(next(it))
        elif a == "--datei": datei = next(it)
        elif a == "--antwort": antwort = next(it)
    # Ohne --datei: <aufgabe>.py. Die Aufgabe ball_zuordnung verlangt
    # gedaechtnis.py - also dort immer --datei mitgeben.
    datei = datei or (aufgabe + ".py")
    if _pruefung_laeuft():
        print("ABBRUCH: Eine Pruefung laeuft (Flagge oder Pruefungsmodus). Danach noch einmal.")
        return 2
    ordner = PROTOKOLLE / ("projektlauf_%s_%s" % (aufgabe, datetime.now().strftime("%Y-%m-%d_%H%M%S")))
    ordner.mkdir(parents=True, exist_ok=True)
    fortschritt = ordner / "FORTSCHRITT.txt"

    def melde(zeile):
        z = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), zeile)
        print(z, flush=True)
        with open(fortschritt, "a", encoding="utf-8") as h:
            h.write(z + "\n")

    e = schleife(aufgabe, datei, modell=modell, runden=runden, melde=melde, protokoll=ordner,
                 antwort=antwort)
    (ordner / "gesamt.json").write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    melde("Protokoll: %s" % ordner)
    return 0 if str(e["ausgang"]).startswith("FERTIG") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
