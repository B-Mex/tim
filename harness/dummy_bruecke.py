#!/usr/bin/env python3
"""Tims Zugang zum Dummy-Pico - und nur zu ihm.

Der Dummy ist die Uebungshardware: ein zweiter Pico W, auf dem dieselbe
Brueckensoftware laeuft wie auf der echten Funkbruecke. Tim darf hier
lernen, was er an der echten Bruecke nicht darf - lauschen, den
Mesh-Schluessel eintragen, Raeume anlernen.

**Die Sicherheitslinie haengt am GERAET, nicht an der Adresse.** Eine IP
kommt vom DHCP und kann morgen einem anderen Geraet gehoeren; die
Chip-ID nicht. Vor jeder Handlung wird deshalb /status abgefragt und die
gemeldete ID gegen DUMMY_ID geprueft. Antwortet etwas anderes - auch
wenn es sich "Funkbruecke" nennt und richtig aussieht - bricht das
Werkzeug ab, ohne etwas zu tun.

Das ist bewusst streng: Die beiden Picos stammen aus derselben Charge,
ihre IDs unterscheiden sich in zwei Ziffern (...c5be gegen ...c5c0). Wer
hier nur hinschaut statt zu vergleichen, verwechselt sie.

Was dieses Werkzeug NICHT kann, und zwar mit Absicht:

* die echte Funkbruecke ansprechen (ID-Riegel, s.o.)
* Firmware aufspielen (das bleibt Handarbeit am USB)
* rohe Funkpakete senden (nur benannte Raeume, wie bei lampen_steuern)

Aufrufe:

    dummy_bruecke.py stand
    dummy_bruecke.py lauschen [sekunden]
    dummy_bruecke.py schluessel <8 Hex-Zeichen>
    dummy_bruecke.py raum <name> <nummer>
    dummy_bruecke.py ausrollen
    dummy_bruecke.py --selbsttest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Die Chip-ID des Dummy - der einzige Pico, den dieses Werkzeug anfassen
# darf. Steht hier im Klartext, weil sie kein Geheimnis ist: Sie steht
# in jeder /status-Antwort und auf dem Geraet selbst.
DUMMY_ID = "28cdc106c5be"

# Die Konfiguration des Uebungsordners (Adresse + Token des Dummy).
ZUGANG = Path.home() / "Desktop" / "M1_DEPLOYMENT" / "hardware" / \
    "dummy_bruecke" / "dummy_zugang.json"

HEX8 = re.compile(r"^[0-9a-fA-F]{8}$")
NAME_MUSTER = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


class KeinDummy(Exception):
    """Am anderen Ende ist nicht der Dummy - es wird nichts getan."""


def zugang_lesen() -> dict:
    if not ZUGANG.is_file():
        raise KeinDummy(
            "dummy_zugang.json fehlt (%s). Der Uebungsordner ist nicht "
            "eingerichtet." % ZUGANG)
    k = json.loads(ZUGANG.read_text(encoding="utf-8"))
    if not k.get("adresse") or not k.get("token"):
        raise KeinDummy("dummy_zugang.json hat keine adresse/token.")
    return k


def _ruf(pfad: str, rumpf: dict | None = None, geduld: float = 8.0) -> dict:
    k = zugang_lesen()
    daten = json.dumps(rumpf).encode("utf-8") if rumpf is not None else None
    anfrage = urllib.request.Request(
        k["adresse"].rstrip("/") + pfad, data=daten,
        headers={"Content-Type": "application/json", "X-Token": k["token"]},
        method="POST" if daten is not None else "GET")
    with urllib.request.urlopen(anfrage, timeout=geduld) as antwort:
        return json.loads(antwort.read().decode("utf-8"))


def dummy_bestaetigen() -> dict:
    """Antwortet wirklich der Dummy? Sonst Abbruch, ohne etwas zu tun."""
    try:
        stand = _ruf("/status")
    except urllib.error.URLError as fehler:
        raise KeinDummy("Dummy nicht erreichbar: %s" % fehler) from fehler
    gemeldet = str(stand.get("id", ""))
    if gemeldet != DUMMY_ID:
        raise KeinDummy(
            "Am anderen Ende antwortet NICHT der Dummy: gemeldete ID %r, "
            "erwartet %r. Abgebrochen - hier wird nichts geschaltet und "
            "nichts geschrieben." % (gemeldet, DUMMY_ID))
    return stand


def befehl_stand() -> int:
    stand = dummy_bestaetigen()
    print("Dummy-Bruecke laeuft: Fassung %s auf %s"
          % (stand.get("version"), stand.get("ip")))
    print("  ID %s (bestaetigt), seit %s s, %s Sendungen, %s dBm"
          % (stand.get("id"), stand.get("laeuft_s"),
             stand.get("gesendet"), stand.get("rssi")))
    raeume = stand.get("raeume") or []
    print("  Raeume: %s" % (", ".join(raeume) if raeume else "(keine)"))
    return 0


def befehl_lauschen(sekunden: float = 25.0) -> int:
    dummy_bestaetigen()
    ms = int(max(1.0, min(25.0, sekunden)) * 1000)
    print("Lausche %d ms - jetzt einen Raum schalten. Es wird nichts "
          "gefunkt, nur zugehoert." % ms)
    e = _ruf("/lauschen", {"ms": ms}, geduld=ms / 1000.0 + 15.0)
    pakete = e.get("pakete", 0)
    gedeutet = e.get("gedeutet", 0)
    print("Gehoert: %s Pakete, davon %s lesbar." % (pakete, gedeutet))
    if e.get("mesh_key"):
        print("  MESH-SCHLUESSEL zurueckgerechnet: %s" % e["mesh_key"])
        print("  (Der eigene Schluessel passt nicht zu den gehoerten "
              "Lampen. Eintragen mit: schluessel %s)" % e["mesh_key"])
    if e.get("raeume"):
        print("  Raumnummern gehoert: %s"
              % ", ".join(str(r) for r in e["raeume"]))
        print("  Benennen mit: raum <name> <nummer>")
    if e.get("bekannt"):
        print("  Davon schon benannt: %s" % json.dumps(e["bekannt"]))
    if e.get("lampen"):
        print("  Einzeln angesprochene Lampen: %s" % e["lampen"])
        print("  (Die tragen keine Raumnummer - in der Hersteller-App "
              "erst einem Raum zuordnen.)")
    if not pakete:
        print("  Nichts gehoert. Wurde in den %d ms wirklich geschaltet?"
              % ms)
    return 0


def befehl_schluessel(wert: str) -> int:
    """Den Mesh-Schluessel in den Uebungsordner schreiben und ausrollen."""
    if not HEX8.match(wert or ""):
        print("FEHLER Der Schluessel besteht aus genau 8 Hex-Zeichen "
              "(0-9, a-f). Bekommen: %r" % wert)
        return 1
    dummy_bestaetigen()
    pfad = ZUGANG.parent / "bruecke_konfig.json"
    if not pfad.is_file():
        print("FEHLER %s fehlt." % pfad)
        return 1
    konfig = json.loads(pfad.read_text(encoding="utf-8"))
    alt = konfig.get("mesh_key")
    konfig["mesh_key"] = wert.lower()
    pfad.write_text(json.dumps(konfig, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print("Schluessel eingetragen: %s -> %s" % (alt, wert.lower()))
    print("Jetzt ausrollen, damit der Dummy ihn benutzt:")
    print("  Aktion dummy_ausrollen")
    return 0


def befehl_raum(name: str, nummer: str) -> int:
    if not NAME_MUSTER.match(name or ""):
        print("FEHLER Raumname: klein, ohne Umlaute, 1-20 Zeichen. "
              "Bekommen: %r" % name)
        return 1
    try:
        n = int(nummer)
    except (TypeError, ValueError):
        print("FEHLER Raumnummer muss eine Zahl sein. Bekommen: %r" % nummer)
        return 1
    dummy_bestaetigen()
    e = _ruf("/raum", {"name": name, "nummer": n})
    if not e.get("ok"):
        print("FEHLER %s" % e.get("fehler", e))
        return 1
    print("Raum gemerkt: %s = %s" % (e.get("gemerkt"), n))
    print("  Raeume jetzt: %s" % json.dumps(e.get("raeume", {}),
                                            ensure_ascii=False))
    return 0


def usb_id_lesen() -> str | None:
    """Die WLAN-MAC des Pico AM USB lesen - ohne Umweg ueber das Netz.

    Ausgerollt wird ueber die USB-Leitung, nicht ueber WLAN. Der
    ID-Riegel aus dummy_bestaetigen() prueft aber das Geraet am NETZ.
    Beides ist normalerweise derselbe Pico - garantiert ist es nicht.
    Wer die echte Bruecke ansteckt, waehrend der Dummy noch im WLAN
    antwortet, wuerde sonst die falsche Hardware beschreiben.

    Gibt die ID zurueck (wie in /status: MAC ohne Trenner) oder None,
    wenn kein Pico am USB haengt.
    """
    sys.path.insert(0, str(Path.home() / "Desktop" / "brmesh-bridge" / "tools"))
    try:
        import pico_draht
    except ImportError:
        return None
    if not pico_draht.anschluss_finden():
        return None
    try:
        with pico_draht.Draht() as draht:
            draht.abbrechen()
            draht.schreiben(
                "import network; "
                "print('MAC', network.WLAN(network.STA_IF)"
                ".config('mac').hex())")
            roh = draht.lesen_bis("MAC ", 8.0)
    except Exception:
        return None
    for zeile in roh.splitlines():
        zeile = zeile.strip()
        if zeile.startswith("MAC ") and len(zeile) > 4:
            return zeile[4:].strip().lower()
    return None


def befehl_ausrollen() -> int:
    """Die Uebungskonfiguration auf den Dummy spielen - nach ID-Pruefung."""
    dummy_bestaetigen()          # Riegel 1: das Geraet im Netz
    am_usb = usb_id_lesen()      # Riegel 2: das Geraet an der Leitung
    if am_usb is None:
        print("ABGEBROCHEN Am USB haengt kein ansprechbarer Pico. "
              "Ausgerollt wird ueber die Leitung - ohne sie geht nichts.")
        return 1
    if am_usb != DUMMY_ID:
        print("ABGEBROCHEN Am USB haengt NICHT der Dummy: gemeldete ID %r, "
              "erwartet %r. Es wird nichts geschrieben." % (am_usb, DUMMY_ID))
        return 1
    print("Beide Riegel offen: Netz und USB melden %s." % DUMMY_ID)
    import subprocess
    ordner = ZUGANG.parent
    ergebnis = subprocess.run(
        [sys.executable, str(ordner / "bruecke_wlan_ausrollen.py")],
        cwd=str(ordner), capture_output=True, text=True, timeout=300)
    print(ergebnis.stdout[-4000:])
    if ergebnis.stderr.strip():
        print("--- Fehlerausgabe ---")
        print(ergebnis.stderr[-2000:])
    return ergebnis.returncode


def selbsttest() -> int:
    fehler = []

    def pruefe(bedingung, text, zusatz=""):
        print("  %-7s %s%s" % ("ok" if bedingung else "FEHLER", text,
                               "" if bedingung else "   <- " + str(zusatz)))
        if not bedingung:
            fehler.append(text)

    print("dummy_bruecke Selbsttest:")

    # Der Kern: Der ID-Riegel muss greifen, nicht nur im Kommentar stehen.
    global _ruf
    echt = _ruf
    try:
        _ruf = lambda p, r=None, geduld=8.0: {  # noqa: E731
            "id": "28cdc106c5c0", "ok": True, "version": "2.6"}
        try:
            dummy_bestaetigen()
            pruefe(False, "die ECHTE Bruecke wird abgewiesen",
                   "sie wurde durchgelassen!")
        except KeinDummy as k:
            pruefe("NICHT der Dummy" in str(k),
                   "die ECHTE Bruecke wird abgewiesen", str(k)[:60])

        _ruf = lambda p, r=None, geduld=8.0: {"id": DUMMY_ID, "ok": True}  # noqa: E731
        try:
            dummy_bestaetigen()
            pruefe(True, "der Dummy wird durchgelassen (Gegenprobe)")
        except KeinDummy as k:
            pruefe(False, "der Dummy wird durchgelassen (Gegenprobe)", k)

        # Ein Geraet ohne ID ist auch nicht der Dummy.
        _ruf = lambda p, r=None, geduld=8.0: {"ok": True}  # noqa: E731
        try:
            dummy_bestaetigen()
            pruefe(False, "Antwort ohne ID wird abgewiesen", "durchgelassen!")
        except KeinDummy:
            pruefe(True, "Antwort ohne ID wird abgewiesen")
    finally:
        _ruf = echt

    # Die Eingabepruefungen - hier darf nichts durchrutschen.
    # Erfundene Werte, KEIN echter Schluessel: Diese Datei ist
    # veroeffentlichungsfaehig, und ein Testwert ist genauso ein Leck wie
    # ein Konfigurationseintrag. Geprueft wird die Form, nicht der Inhalt.
    for boese in ("", "xyz", "abcdef1", "abcdef123", "abcdef12; rm -rf /",
                  "../../etc", None):
        pruefe(befehl_schluessel(boese) == 1,
               "Schluessel abgewiesen: %r" % boese)
    for boese in ("", "Buero", "bü", "a" * 21, "raum;rm", "1raum"):
        pruefe(befehl_raum(boese, "3") == 1, "Raumname abgewiesen: %r" % boese)
    pruefe(befehl_raum("buero", "keine_zahl") == 1,
           "Raumnummer 'keine_zahl' abgewiesen")

    print("\n%s" % ("Alle Pruefungen bestanden."
                    if not fehler else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--hilfe", "-h", "help"):
        print(__doc__)
        return 0
    if args[0] == "--selbsttest":
        return selbsttest()
    try:
        if args[0] == "stand":
            return befehl_stand()
        if args[0] == "lauschen":
            return befehl_lauschen(float(args[1]) if len(args) > 1 else 25.0)
        if args[0] == "schluessel":
            return befehl_schluessel(args[1] if len(args) > 1 else "")
        if args[0] == "ausrollen":
            return befehl_ausrollen()
        if args[0] == "raum":
            if len(args) < 3:
                print("FEHLER raum braucht Name und Nummer.")
                return 1
            return befehl_raum(args[1], args[2])
    except KeinDummy as fehler:
        print("ABGEBROCHEN %s" % fehler)
        return 1
    except urllib.error.URLError as fehler:
        print("FEHLER Dummy nicht erreichbar: %s" % fehler)
        return 1
    print("FEHLER Unbekannter Befehl: %s" % args[0])
    return 1


if __name__ == "__main__":
    sys.exit(main())
