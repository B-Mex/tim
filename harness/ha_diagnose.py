#!/usr/bin/env python3
"""Home-Assistant-Diagnose - nur lesend.

Warum es dieses Werkzeug gibt: In der Nacht auf den 24.08.2026 stand die
Frage "gehen die Esszimmer- und Kuechenkachel noch?" an - beantwortet
wurde sie von Hand ueber die HA-API: Kacheln pruefen, BRMesh-Entitaeten
zaehlen, Benachrichtigungen lesen, Geister-Entitaeten einordnen. Genau
diesen Blick bekommt Tim hier als Knopf, damit er die Frage kuenftig
selbst beantworten kann.

Die Sicherheitslinie: Dieses Werkzeug LIEST nur. Es schaltet nichts,
loescht nichts und schreibt nichts nach Home Assistant - jeder Abruf
ist ein GET, und der Selbsttest haelt genau das fest (der Doppelgaenger-
Server zaehlt mit, was bei ihm ankommt). Home Assistant und Tim bleiben
unabhaengig: Ist HA nicht erreichbar, meldet die Diagnose das ehrlich
als Befund, statt selbst zu stolpern.

Wissen aus der Nacht, das hier eingebaut ist:

  * Die Shelly-Kacheln (Esszimmer/Kueche) sind PULS-GEBER fuer
    Stromstossrelais: auto_off nach 0,07 s. Ihr Zustand "aus" ist
    deshalb RICHTIG und darf nie "repariert" werden.
  * BRMesh-Lampen sind waehrend der ~10 s Hochlaufzeit des Pico ehrlich
    "nicht verfuegbar" - erst danach ist es ein Problem.
  * Diese HA-Version schreibt kein home-assistant.log nach /config.
    Fehler stehen in den BENACHRICHTIGUNGEN - deshalb liest die
    Diagnose genau die.
  * "Geister-Entitaeten" (nicht verfuegbar, aus ausgebauten YAML-Teilen)
    sind nach einem Umbau normal und kein Fehler - sie werden gezaehlt
    und eingeordnet, nicht angeprangert.

Aufruf:
    python3 ha_diagnose.py                  # Diagnose gegen das echte HA
    python3 ha_diagnose.py --selbsttest     # Pruefungen ohne echtes HA

Exit: 0 = in Ordnung (Hinweise erlaubt), 1 = Probleme gefunden,
      2 = LUECKE (kein Token eingerichtet - keine Aussage moeglich).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HA_TOKEN_DATEI = Path("/opt/ki-server/config/ha_token.secret")
# homeassistant.local loest ueber mDNS auf; wer eine feste Adresse will,
# setzt M1_HA_ADRESSE (z.B. in der plist des Job-Servers).
STANDARD_ADRESSE = os.environ.get("M1_HA_ADRESSE",
                                  "http://homeassistant.local:8123")
ZEITGRENZE = 8


def token_laden(pfad: Path = HA_TOKEN_DATEI) -> str | None:
    """Erste Nicht-Kommentar-Zeile der Secret-Datei - oder None."""
    try:
        text = pfad.read_text(encoding="utf-8")
    except OSError:
        return None
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if zeile and not zeile.startswith("#"):
            return zeile
    return None


def _get(adresse: str, pfad: str, token: str):
    """Ein GET an die HA-API. Rueckgabe (daten, None) oder (None, grund).

    Bewusst die EINZIGE Stelle, die Anfragen baut: So gibt es genau
    einen Ort, an dem die Nur-Lesen-Linie haengt - und der Selbsttest
    prueft sie am laufenden Doppelgaenger, nicht am Kommentar.
    """
    anfrage = urllib.request.Request(
        adresse.rstrip("/") + pfad,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(anfrage, timeout=ZEITGRENZE) as antwort:
            return json.loads(antwort.read().decode("utf-8")), None
    except urllib.error.HTTPError as fehler:
        return None, "HTTP %s" % fehler.code
    except Exception as fehler:  # URLError, Timeout, kaputtes JSON
        return None, str(fehler)


def einordnen(zustaende: list) -> tuple[list, list, list]:
    """Die reine Bewertung der Entitaeten - ohne Netz, damit pruefbar.

    Rueckgabe: (probleme, hinweise, zeilen fuer den Bericht).
    """
    probleme: list[str] = []
    hinweise: list[str] = []
    zeilen: list[str] = []

    shellys = [z for z in zustaende
               if z.get("entity_id", "").startswith("switch.shelly")]
    lampen = [z for z in zustaende
              if z.get("entity_id", "").startswith("light.brmesh_bridge_")]
    tempo = [z for z in zustaende
             if z.get("entity_id", "") == "number.brmesh_bridge_disko_tempo"]
    meldungen = [z for z in zustaende
                 if z.get("entity_id", "").startswith("persistent_notification.")]
    geister = [z for z in zustaende if z.get("state") == "unavailable"]

    # --- Shelly-Kacheln (Esszimmer/Kueche) ---
    if not shellys:
        hinweise.append("Keine Shelly-Schalter gefunden.")
    for s in shellys:
        eid, stand = s.get("entity_id"), s.get("state")
        if stand == "unavailable":
            probleme.append("Kachel %s ist nicht verfuegbar." % eid)
        else:
            # "aus" ist bei den Puls-Gebern der Sollzustand (auto_off
            # 0,07 s fuer die Stromstossrelais) - nie "reparieren".
            zeilen.append("  ok      Kachel %s: %s (aus ist hier richtig - "
                          "Puls-Geber)" % (eid, stand))

    # --- BRMesh-Lampen ---
    kaputt = [l for l in lampen if l.get("state") == "unavailable"]
    if not lampen:
        hinweise.append("Keine BRMesh-Lampen-Entitaeten gefunden - "
                        "Integration nicht eingerichtet?")
    elif kaputt:
        probleme.append(
            "%d von %d BRMesh-Lampen nicht verfuegbar (%s). Falls der "
            "Pico gerade neu startet, ist das seine ~10 s Hochlaufzeit - "
            "sonst Bruecke pruefen (Aktion funkbruecke_wlan)."
            % (len(kaputt), len(lampen),
               ", ".join(k.get("entity_id", "?") for k in kaputt[:5])))
    else:
        zeilen.append("  ok      BRMesh-Lampen: alle %d verfuegbar"
                      % len(lampen))

    if tempo:
        stand = tempo[0].get("state")
        if stand == "unavailable":
            probleme.append("Disko-Tempo-Regler nicht verfuegbar.")
        else:
            zeilen.append("  ok      Disko-Tempo-Regler: %s %%" % stand)

    # --- Benachrichtigungen: hier stehen die Konfigurationsfehler ---
    for m in meldungen:
        titel = (m.get("attributes") or {}).get("title") or m.get("entity_id")
        probleme.append("Benachrichtigung in HA: %s" % titel)
    if not meldungen:
        zeilen.append("  ok      keine Fehler-Benachrichtigungen")

    # --- Geister-Entitaeten: einordnen, nicht anprangern ---
    fremde = [g for g in geister
              if not g.get("entity_id", "").startswith(
                  ("light.brmesh_bridge_", "switch.shelly",
                   "number.brmesh_bridge_"))]
    if fremde:
        hinweise.append(
            "%d weitere Entitaeten 'nicht verfuegbar' (z.B. %s) - nach "
            "einem YAML-Ausbau sind das meist Geister-Eintraege der "
            "Registry; loeschen kann sie Mexla unter Einstellungen -> "
            "Entitaeten, Filter 'wiederhergestellt'."
            % (len(fremde),
               ", ".join(f.get("entity_id", "?") for f in fremde[:3])))

    return probleme, hinweise, zeilen


def diagnose(adresse: str, token: str) -> int:
    """Der volle Blick auf Home Assistant. Gibt den Exit-Code zurueck."""
    print("Home-Assistant-Diagnose (nur lesend) gegen %s" % adresse)

    lebt, grund = _get(adresse, "/api/", token)
    if lebt is None:
        print("PROBLEM Home Assistant nicht erreichbar: %s" % grund)
        print("\nErgebnis: 1 Problem. Tim laeuft davon unabhaengig weiter.")
        return 1

    konfig, grund = _get(adresse, "/api/config", token)
    if konfig:
        print("  ok      API antwortet, Version %s, Zustand %s"
              % (konfig.get("version", "?"), konfig.get("state", "?")))

    zustaende, grund = _get(adresse, "/api/states", token)
    if zustaende is None:
        print("PROBLEM Entitaeten nicht lesbar: %s" % grund)
        return 1

    probleme, hinweise, zeilen = einordnen(zustaende)
    for z in zeilen:
        print(z)
    for h in hinweise:
        print("  HINWEIS %s" % h)
    for p in probleme:
        print("  PROBLEM %s" % p)

    if probleme:
        print("\nErgebnis: %d Problem(e), %d Hinweis(e)."
              % (len(probleme), len(hinweise)))
        return 1
    print("\nErgebnis: in Ordnung (%d Hinweis(e))." % len(hinweise))
    return 0


# ----------------------------------------------------------------------
# Selbsttest - ohne echtes Home Assistant
# ----------------------------------------------------------------------
def _selbsttest() -> int:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, ("  [%s]" % zusatz) if zusatz else ""))
            fehler += 1

    print("ha_diagnose Selbsttest:")

    # --- Die reine Bewertung, beide Seiten (Zwei-Seiten-Beweis) ---
    gesund = [
        {"entity_id": "switch.shelly_esszimmer", "state": "off"},
        {"entity_id": "light.brmesh_bridge_buero", "state": "on"},
        {"entity_id": "light.brmesh_bridge_kueche", "state": "off"},
        {"entity_id": "number.brmesh_bridge_disko_tempo", "state": "40"},
        {"entity_id": "camera.garage", "state": "unavailable"},
    ]
    p, h, _ = einordnen(gesund)
    pruefe(not p, "gesunder Zustand: keine Probleme", str(p))
    pruefe(any("Geister" in x for x in h),
           "fremde unavailable-Entitaet wird als Geister-Hinweis eingeordnet")

    krank = [
        {"entity_id": "switch.shelly_esszimmer", "state": "unavailable"},
        {"entity_id": "light.brmesh_bridge_buero", "state": "unavailable"},
        {"entity_id": "persistent_notification.fehler",
         "state": "notifying",
         "attributes": {"title": "Ungueltige Konfiguration"}},
    ]
    p, h, _ = einordnen(krank)
    pruefe(any("Kachel" in x for x in p), "tote Kachel wird zum Problem")
    pruefe(any("BRMesh" in x for x in p), "tote Lampe wird zum Problem")
    pruefe(any("Ungueltige Konfiguration" in x for x in p),
           "Benachrichtigung wird gemeldet")
    pruefe(len(p) == 3, "kranker Zustand: genau 3 Probleme", str(len(p)))

    # Die Puls-Geber-Falle: "aus" darf NIE als Problem gelten.
    aus = [{"entity_id": "switch.shelly_kueche", "state": "off"}]
    p, _, z = einordnen(aus)
    pruefe(not p, "Kachel 'aus' ist KEIN Problem (Puls-Geber)")
    pruefe(any("Puls-Geber" in x for x in z),
           "der Bericht erklaert, warum 'aus' richtig ist")

    # --- Doppelgaenger-Server: nur GETs, Ende-zu-Ende-Exit-Codes ---
    gesehen = {"methoden": []}

    class _FakeHA(BaseHTTPRequestHandler):
        szenario = gesund

        def _sende(self, daten):
            roh = json.dumps(daten).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)

        def do_GET(self):  # noqa: N802
            gesehen["methoden"].append("GET")
            if self.path == "/api/":
                self._sende({"message": "API running."})
            elif self.path == "/api/config":
                self._sende({"version": "2026.8", "state": "RUNNING"})
            elif self.path == "/api/states":
                self._sende(type(self).szenario)
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            gesehen["methoden"].append("POST")
            self.send_error(405)

        def log_message(self, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHA)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    adresse = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        code = diagnose(adresse, "probe-token")
        pruefe(code == 0, "gesundes HA ergibt Exit 0", str(code))
        _FakeHA.szenario = krank
        code = diagnose(adresse, "probe-token")
        pruefe(code == 1, "krankes HA ergibt Exit 1 (Gegenprobe)", str(code))
        pruefe(gesehen["methoden"]
               and all(m == "GET" for m in gesehen["methoden"]),
               "beim Doppelgaenger kamen ausschliesslich GETs an",
               str(set(gesehen["methoden"])))
    finally:
        server.shutdown()
        server.server_close()

    # --- Erreichbarkeit: ein toter Server ist Exit 1, kein Absturz ---
    code = diagnose("http://127.0.0.1:1", "probe-token")
    pruefe(code == 1, "nicht erreichbares HA ergibt Exit 1", str(code))

    # --- Token-Luecke: ehrliche LUECKE statt Behauptung ---
    kein = token_laden(Path("/gibt/es/nicht/ha_token.secret"))
    pruefe(kein is None, "fehlende Token-Datei ergibt None")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".secret",
                                     delete=False) as t:
        t.write("# Kommentar\n\nechter-wert\n")
        tname = t.name
    try:
        pruefe(token_laden(Path(tname)) == "echter-wert",
               "Token-Datei: Kommentare und Leerzeilen werden uebergangen")
    finally:
        os.unlink(tname)

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlle Pruefungen bestanden.")
    return fehler


def main(argumente: list[str]) -> int:
    if "--selbsttest" in argumente:
        return _selbsttest()

    token = token_laden()
    if not token:
        print("LUECKE: %s fehlt oder ist leer - ohne Token keine Aussage "
              "ueber Home Assistant. Einrichten: siehe "
              "config/ha_token.secret.example." % HA_TOKEN_DATEI)
        return 2
    return diagnose(STANDARD_ADRESSE, token)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
