#!/usr/bin/env python3
"""Setzt config/autonomie.conf tatsaechlich durch (vorher war die Datei reine Doku).

Jede autonome Aktion MUSS vorher pruefe_aktion() aufrufen. Die Funktion
antwortet mit (erlaubt, begruendung) - bei False wird die Aktion nicht
ausgefuehrt, sondern nur als Vorschlag protokolliert.

Grenzen der Durchsetzung (ehrlich): Das hier bremst den Harness und die
Scripts, die es aufrufen. Es ist KEIN Sandkasten. Wenn du dem lokalen
Modell ueber die Claude Code CLI direkten Shell-Zugriff gibst, kann es an
dieser Bremse vorbei - dagegen hilft nur der Kill-Switch bzw. dem Modell
keinen Shell-Zugriff zu geben.
"""

import os
import re
import sys
from pathlib import Path

CONF_KANDIDATEN = [
    Path.home() / "Desktop" / "M1_DEPLOYMENT" / "config" / "autonomie.conf",
    Path("/opt/ki-server/config/autonomie.conf"),
    Path("/Volumes/M1_DEPLOYMENT/config/autonomie.conf"),
    Path(__file__).parent.parent / "config" / "autonomie.conf",
]

# WICHTIG: Diese Liste MUSS jeden Ort abdecken, an den "m1-stop" schreiben
# kann - sonst meldet der Kill-Switch Erfolg, wirkt aber nicht. m1-stop nutzt
# m1_deploy_dir() aus config/zshrc_ergaenzung.txt; deren Kandidaten stehen
# hier alle mit drin. Der Selbsttest unten haelt beide Listen zusammen.
STOP_ORDNER = [
    Path.home() / "Desktop" / "M1_DEPLOYMENT",
    Path("/Volumes/M1_DEPLOYMENT"),
    Path("/Volumes/Extreme SSD/M1_DEPLOYMENT"),
    Path("/Volumes/SanDisk/M1_DEPLOYMENT"),
    Path("/Volumes/SANDISK/M1_DEPLOYMENT"),
    Path("/opt/ki-server"),
    # Betrieb direkt aus dem Projektordner (z.B. von der SSD, ohne Install)
    Path(__file__).parent.parent,
]
STOP_KANDIDATEN = [ordner / "STOP" for ordner in STOP_ORDNER]

# Bereiche, die IMMER verboten bleiben - auch im Modus "autonom".
HARTE_GRENZEN = {
    "kauf": "NIEMALS_KAEUFE",
    "netzspannung": "NIEMALS_NETZSPANNUNG",
    "loeschen": "NIEMALS_LOESCHEN_OHNE_BACKUP",
}

# Bereich -> Schalter in autonomie.conf
#
# Nur noch EIN Eintrag, seit dem 29.08.2026. Vorher standen hier sechs -
# aber fuenf davon bewachten nichts: pruefe_aktion() wird im ganzen
# Projekt an genau zwei Stellen aufgerufen (m1_zentrale.shell_erlaubt
# und crew_generic.job_ausfuehren), und die zweite schreibt im eigenen
# Code, dass der Ablauf ohnehin nur Text liefert und nichts ausfuehrt.
# "software_install", "ha_konfig", "hardware_treiber", "reauth" und
# "netzwerk" waren also Absichtserklaerungen, keine Riegel.
#
# Warum das schaedlich war und nicht bloss ueberfluessig: Eine
# Oberflaeche, die sechs Schalter zeigt, verspricht sechs Riegel. Wer
# "erlaube ha konfig" auf nein stellt, glaubt, damit etwas verhindert
# zu haben - und stellt die Frage nicht mehr. Ein Riegel ohne Tuer
# macht die Anlage unehrlicher, nicht sicherer.
#
# Alles, was die fuenf versprachen, laeuft heute ueber die Shell - und
# fuer die gibt es ERLAUBE_SHELL. Kommt spaeter ein echter
# HA-Konfig-Weg dazu, kommt sein Riegel MIT dem Weg zusammen, nicht auf
# Vorrat.
BEREICH_SCHALTER = {
    # Freier Shell-Zugriff - fuer Mexlas Shell-Ansicht und (seit dem
    # 29.08.2026) fuer Tims Chat-Werkzeug shell_befehl. Maechtiger als
    # alles andere zusammen, deshalb ein eigener, ausdruecklicher
    # Schalter.
    "shell": "ERLAUBE_SHELL",
}

# Schalter, die es frueher gab. Sie werden beim Zuruecksetzen auf den
# Normalzustand weiterhin auf "nein" gestellt, damit eine alte conf
# nicht mit stillen "ja"-Zeilen liegen bleibt - aber sie schalten
# nichts mehr frei, weil kein Bereich mehr auf sie zeigt.
ABGESCHAFFTE_SCHALTER = ("ERLAUBE_SOFTWARE_INSTALL", "ERLAUBE_HA_KONFIG",
                         "ERLAUBE_HARDWARE_TREIBER", "ERLAUBE_REAUTH",
                         "ERLAUBE_NETZWERK")

# Schalter, die ueber Tim umgelegt werden duerfen (Job-Server-Aktion
# "autonomie_setzen"). Bewusst NICHT dabei: die NIEMALS_*-Grenzen, die im
# Code fest verdrahtet sind (HARTE_GRENZEN); ein Schalter dafuer waere
# eine Attrappe, weil pruefe_aktion() sie ohnehin nie freigibt.
SETZBARE_SCHALTER = tuple(BEREICH_SCHALTER.values())

# Der Modus war bis 22.08.2026 bewusst nur an der Tastatur zu aendern.
# Mexla wollte ihn in der Oberflaeche haben - mit denselben Riegeln wie der
# Not-Aus: Herunterstufen geht immer, Hochstufen nur ohne Kill-Switch
# (das entscheidet der Job-Server, siehe m1_job_server.py).
MODI = ("safe", "assist", "autonom")
# Je hoeher, desto mehr darf der Harness selbst. "safe" ist der
# Auslieferungs- und Normalzustand.
MODUS_STUFE = {"safe": 0, "assist": 1, "autonom": 2}

# Der Normalzustand, auf den der Knopf "alles zurueck auf sicher" stellt:
# genau der Auslieferungszustand dieser Anlage.
NORMALZUSTAND = {"AUTONOMIE_MODUS": "safe",
                 **{s: "nein" for s in SETZBARE_SCHALTER},
                 # Die abgeschafften auch - siehe ABGESCHAFFTE_SCHALTER.
                 **{s: "nein" for s in ABGESCHAFFTE_SCHALTER}}


def modus_pruefen(modus: str) -> str:
    """Gibt den bereinigten Modus zurueck oder loest SystemExit aus.

    Bewusst getrennt vom Setzen: Der Selbsttest kann die Pruefung so
    durchspielen, OHNE in die laufende autonomie.conf zu schreiben. Am
    22.08.2026 hat genau das gefehlt - der Test setzte den Modus dabei
    versehentlich auf "autonom".
    """
    bereinigt = (modus or "").strip().lower()
    if bereinigt not in MODI:
        raise SystemExit(
            f"FEHLER: Modus muss einer von {', '.join(MODI)} sein, nicht '{modus}'.")
    return bereinigt


def modus_setzen(modus: str) -> None:
    """Setzt AUTONOMIE_MODUS in allen conf-Kopien."""
    _in_allen_conf("AUTONOMIE_MODUS", modus_pruefen(modus))


def normalzustand_setzen() -> None:
    """Alles zurueck auf den sicheren Auslieferungszustand.

    Bewusst EIN Knopf: Wer nach einem Versuch aufraeumen will, soll nicht
    sechs Schalter einzeln zuruecklegen und dabei einen vergessen.
    """
    for schluessel, wert in NORMALZUSTAND.items():
        _in_allen_conf(schluessel, wert)
    print("Normalzustand hergestellt: " +
          ", ".join(f"{k}={v}" for k, v in NORMALZUSTAND.items()))


def _wert_setzen(text: str, schalter: str, wert: str) -> str:
    """Ersetzt im conf-Text nur den Wert eines Schalters.

    Kommentare und Ausrichtung der Zeile bleiben erhalten. Fehlt die
    Zeile, wird sie angefuegt - so wandert ein neuer Schalter auch in
    eine aeltere conf-Kopie.
    """
    muster = re.compile(
        rf"^({re.escape(schalter)}\s*=\s*)[^#\n]*?(\s*(?:#.*)?)$",
        re.MULTILINE)
    if muster.search(text):
        return muster.sub(lambda m: m.group(1) + wert + m.group(2), text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{schalter}={wert}\n"


def _in_allen_conf(schluessel: str, wert: str) -> list:
    """Schreibt einen Wert in ALLE vorhandenen conf-Kopien.

    Alle Kopien, weil sonst Anzeige und Durchsetzung auseinanderlaufen:
    die Zentrale liest fest /opt/ki-server/config, lade_config() nimmt
    den ersten Treffer aus CONF_KANDIDATEN (Desktop zuerst).
    """
    geschrieben = []
    gesehen = set()
    for pfad in CONF_KANDIDATEN:
        try:
            if not pfad.is_file():
                continue
            # Zwei Kandidaten koennen auf dieselbe Datei zeigen (der
            # Ordner neben autonomie.py IST /opt/ki-server/config).
            echt = pfad.resolve()
            if echt in gesehen:
                continue
            gesehen.add(echt)
            alt = pfad.read_text(encoding="utf-8", errors="replace")
            neu = _wert_setzen(alt, schluessel, wert)
            if neu != alt:
                tmp = pfad.with_name(pfad.name + ".neu")
                tmp.write_text(neu, encoding="utf-8")
                os.replace(tmp, pfad)
            geschrieben.append(str(pfad))
        except OSError as e:
            print(f"WARNUNG: {pfad} nicht schreibbar ({e})")
    if not geschrieben:
        raise SystemExit("FEHLER: keine autonomie.conf gefunden.")
    print(f"{schluessel}={wert} gesetzt in: " + ", ".join(geschrieben))
    return geschrieben


def schalter_setzen(schalter: str, wert: str) -> None:
    """Setzt einen freigegebenen ja/nein-Schalter."""
    if schalter not in SETZBARE_SCHALTER:
        raise SystemExit(
            f"FEHLER: '{schalter}' ist nicht per Schalter setzbar. "
            f"Erlaubt: {', '.join(SETZBARE_SCHALTER)}")
    if wert not in ("ja", "nein"):
        raise SystemExit(f"FEHLER: Wert muss 'ja' oder 'nein' sein, nicht '{wert}'.")
    _in_allen_conf(schalter, wert)


def _conf_datei():
    for p in CONF_KANDIDATEN:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def lade_config() -> dict:
    """Liest autonomie.conf. Fehlt sie, gilt der sicherste Fall: safe/alles nein."""
    conf = {"AUTONOMIE_MODUS": "safe"}
    datei = _conf_datei()
    if not datei:
        return conf
    try:
        inhalt = datei.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        # Unlesbar (Rechte, Verzeichnis, defekter Datentraeger): nicht
        # abstuerzen, sondern auf den sichersten Fall zurueckfallen. Ein
        # Absturz hier wuerde auch "m1-autonomie" unbrauchbar machen -
        # genau dann, wenn man den Stand wissen will.
        print(f"WARNUNG: {datei} nicht lesbar ({e}) - es gilt safe/alles nein.")
        return conf
    for zeile in inhalt.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        schluessel, _, wert = zeile.partition("=")
        conf[schluessel.strip()] = wert.split("#")[0].strip()
    return conf


def killswitch_aktiv():
    """Pfad der STOP-Datei, falls vorhanden - sonst None."""
    for p in STOP_KANDIDATEN:
        try:
            # is_symlink() zusaetzlich: bei einem Symlink ins Leere liefert
            # exists() False. Eine vorhandene STOP-Markierung zaehlt aber,
            # auch wenn ihr Ziel fehlt - im Zweifel stoppen.
            if p.exists() or p.is_symlink():
                return p
        except OSError:
            # Unlesbarer Pfad (z.B. Laufwerk gerade abgezogen): als aktiv
            # werten waere falsch, weitersuchen ist richtig.
            continue
    return None


def pruefe_aktion(bereich: str) -> tuple:
    """Darf eine Aktion dieses Bereichs jetzt ausgefuehrt werden?

    Rueckgabe: (erlaubt: bool, begruendung: str)
    """
    stop = killswitch_aktiv()
    if stop:
        return False, f"Kill-Switch aktiv ({stop}) - alle autonomen Aktionen gestoppt"

    conf = lade_config()

    # Harte Grenzen zuerst - gelten in JEDEM Modus.
    # Runde 3: Vorher galt die Grenze nur, solange die conf den Schalter
    # auf "ja" liess. Stand dort "nein", fiel die Pruefung durch bis zu
    # BEREICH_SCHALTER - und blockierte nur noch deshalb, weil "kauf",
    # "netzspannung" und "loeschen" dort zufaellig nicht eingetragen sind.
    # Wer die Liste spaeter um einen Einkaufs-Ablauf ergaenzt, haette die
    # "harte" Grenze unbemerkt aufgehoben. "NIEMALS" heisst jetzt niemals:
    # eine conf-Zeile kann diese drei Bereiche nicht freischalten.
    if bereich in HARTE_GRENZEN:
        schalter = HARTE_GRENZEN[bereich]
        if conf.get(schalter, "ja").lower() != "ja":
            return False, (
                f"{schalter} steht auf '{conf.get(schalter)}', bleibt aber "
                "gesperrt - harte Grenzen sind per conf nicht abschaltbar"
            )
        return False, f"{schalter}=ja - harte Grenze, gilt auch im Modus 'autonom'"

    modus = conf.get("AUTONOMIE_MODUS", "safe").lower()
    if modus == "safe":
        return False, "AUTONOMIE_MODUS=safe - es werden nur Vorschlaege gemacht, nichts ausgefuehrt"

    schalter = BEREICH_SCHALTER.get(bereich)
    if schalter is None:
        # Unbekannter Bereich -> im Zweifel nicht ausfuehren
        return False, f"Unbekannter Bereich '{bereich}' - im Zweifel blockiert"

    if conf.get(schalter, "nein").lower() != "ja":
        return False, f"{schalter}=nein - Bereich nicht freigeschaltet"

    if modus == "assist":
        return False, f"AUTONOMIE_MODUS=assist - '{bereich}' ist kritisch, bitte Mexla fragen"

    return True, f"Modus '{modus}' + {schalter}=ja"


def status_text() -> str:
    conf = lade_config()
    datei = _conf_datei()
    zeilen = [
        f"Config:  {datei if datei else 'NICHT GEFUNDEN (Fallback: safe/alles nein)'}",
        f"Modus:   {conf.get('AUTONOMIE_MODUS', 'safe')}",
        f"Kill-Switch: {killswitch_aktiv() or 'nicht aktiv'}",
    ]
    for bereich in BEREICH_SCHALTER:
        erlaubt, grund = pruefe_aktion(bereich)
        zeilen.append(f"  {bereich:20s} {'ERLAUBT' if erlaubt else 'blockiert'}  ({grund})")
    for bereich in HARTE_GRENZEN:
        erlaubt, grund = pruefe_aktion(bereich)
        zeilen.append(f"  {bereich:20s} {'ERLAUBT' if erlaubt else 'blockiert'}  ({grund})")
    return "\n".join(zeilen)


def _script_ordner():
    """Wo liegen die Shell-Scripte? Erster Treffer gewinnt.

    Runde 3: Vorher wurde fest Path(__file__).parent.parent/"scripts"
    genommen. Nach der Installation liegt autonomie.py aber unter
    /opt/ki-server/harness/, und 12_MAC_harness_setup.sh kopiert scripts/
    NICHT dorthin (nur harness/*.py, harness/jobs/*.json, autonomie.conf).
    Der Selbsttest scheiterte deshalb IMMER mit Exit 1 - und weil der
    Installer unter "set -e" laeuft und autonomie.py in Schritt [5/5] vor
    der Cron-Einrichtung aufruft, brach die Installation an dieser Stelle
    ab, bevor je eine crontab-Zeile entstand. Ausserdem meldete
    "m1-autonomie" seitdem dauerhaft einen Fehlalarm.
    """
    kandidaten = [
        Path(__file__).parent.parent / "scripts",
        Path("/opt/ki-server/scripts"),
        Path.home() / "Desktop" / "M1_DEPLOYMENT" / "scripts",
        Path("/Volumes/M1_DEPLOYMENT/scripts"),
        Path("/Volumes/Extreme SSD/M1_DEPLOYMENT/scripts"),
        Path("/Volumes/SanDisk/M1_DEPLOYMENT/scripts"),
        Path("/Volumes/SANDISK/M1_DEPLOYMENT/scripts"),
    ]
    for k in kandidaten:
        try:
            if (k / "4_MAC_watchdog.sh").is_file():
                return k
        except OSError:
            continue
    return None


def _funktionsrumpf(inhalt: str, funktion: str):
    """Gibt den Rumpf einer Shell-Funktion zurueck, ohne Kommentarzeilen.

    Warum noetig: Die Fassung aus Runde 2 suchte die STOP-Pfade per
    Textsuche IRGENDWO in der Datei. Damit bestand der Test auch dann,
    wenn die Pruefung gar nicht mehr existierte und die Pfade nur noch in
    einem Kommentar standen - genau ein Test, der aus dem falschen Grund
    besteht. Jetzt zaehlt nur, was im Rumpf der Funktion steht.
    """
    marke = funktion + "() {"
    if marke not in inhalt:
        return None
    rest = inhalt[inhalt.index(marke) + len(marke):]
    tiefe = 1
    ende = 0
    for pos, zeichen in enumerate(rest):
        if zeichen == "{":
            tiefe += 1
        elif zeichen == "}":
            tiefe -= 1
            if tiefe == 0:
                ende = pos
                break
    rumpf = rest[:ende] if ende else rest
    return "\n".join(z for z in rumpf.splitlines()
                      if not z.lstrip().startswith("#"))


def _pruefe_stop_gleichlauf() -> list:
    """Haelt die STOP-Orte zwischen Python und den Shell-Scripten zusammen.

    Warum als Test: Der Kill-Switch ist ueber sechs Dateien verteilt. Beim
    Review am 2026-08-18 wichen drei Listen voneinander ab - "m1-stop"
    meldete Erfolg, "m1-health" meldete "nicht aktiv", und der Watchdog
    startete Dienste weiter neu. Das faellt im Betrieb erst auf, wenn man
    den Kill-Switch braucht. Also lieber hier hart pruefen.

    Geprueft wird dreierlei, nicht nur die blosse Anwesenheit eines Textes:
      1. die Funktion m1_stop_aktiv() existiert,
      2. sie wird auch AUFGERUFEN (sonst haengt sie wirkungslos herum),
      3. jeder Pflicht-Ort steht in ihrem RUMPF - Kommentare zaehlen nicht.
    """
    ordner = _script_ordner()
    if ordner is None:
        # Kein Fehler: nach der Installation liegt harness/ ohne scripts/
        # unter /opt/ki-server. Nur melden, nicht scheitern - sonst bricht
        # der Installer unter "set -e" ab.
        return []

    zu_pruefen = {
        ordner / "4_MAC_watchdog.sh": "Watchdog (laeuft als root, alle 5 Min)",
        ordner / "10_MAC_healthcheck.sh": "Health-Check (zeigt den Stand an)",
        ordner / "13_MAC_laufwerk_backup.sh": "Laufwerk-Backup (kopiert 34 GB)",
    }
    # Die Orte, die in JEDEM Shell-Script vorkommen muessen. /Users/* deckt
    # den Fall ab, dass das Script als root laeuft ($HOME waere /var/root).
    pflicht = [
        "/opt/ki-server/STOP",
        "$HOME/Desktop/M1_DEPLOYMENT/STOP",
        "/Users/*/Desktop/M1_DEPLOYMENT/STOP",
        "/Volumes/M1_DEPLOYMENT/STOP",
        "/Volumes/Extreme SSD/M1_DEPLOYMENT/STOP",
        "/Volumes/SanDisk/M1_DEPLOYMENT/STOP",
        "/Volumes/SANDISK/M1_DEPLOYMENT/STOP",
    ]
    probleme = []
    for datei, zweck in zu_pruefen.items():
        if not datei.exists():
            probleme.append(f"{datei.name} nicht gefunden ({zweck})")
            continue
        inhalt = datei.read_text(encoding="utf-8", errors="replace")
        rumpf = _funktionsrumpf(inhalt, "m1_stop_aktiv")
        if rumpf is None:
            probleme.append(
                f"{datei.name} hat keine Funktion m1_stop_aktiv() mehr - {zweck}"
            )
            continue
        # Wird sie auch aufgerufen? Ein Vorkommen ist die Definition selbst.
        if inhalt.count("m1_stop_aktiv") < 2:
            probleme.append(
                f"{datei.name} definiert m1_stop_aktiv(), ruft die Funktion "
                f"aber nie auf - der Kill-Switch waere wirkungslos ({zweck})"
            )
        for ort in pflicht:
            if ort not in rumpf:
                probleme.append(
                    f"{datei.name} prueft '{ort}' nicht im Rumpf von "
                    f"m1_stop_aktiv() - {zweck}"
                )

    # Und umgekehrt: jeder Pflicht-Ort muss auch in STOP_KANDIDATEN stehen
    # (ausser /Users/*, das ist die Shell-Schreibweise fuer Path.home()).
    bekannt = {str(k).replace(chr(92), "/") for k in STOP_KANDIDATEN}
    for ort in pflicht:
        if ort.startswith("/Volumes") or ort == "/opt/ki-server/STOP":
            if not any(k.endswith(ort) or k == ort for k in bekannt):
                probleme.append(f"STOP_KANDIDATEN kennt '{ort}' nicht")
    return probleme

if __name__ == "__main__":
    # Unterbefehl "setzen": ein einzelner ja/nein-Schalter, sonst nichts.
    # Wird vom Job-Server (Aktion "autonomie_setzen") aufgerufen; die
    # Pruefung, WAS setzbar ist, liegt allein hier in SETZBARE_SCHALTER.
    if len(sys.argv) >= 2 and sys.argv[1] == "setzen":
        if len(sys.argv) != 4:
            raise SystemExit("Aufruf: autonomie.py setzen <SCHALTER> <ja|nein>")
        schalter_setzen(sys.argv[2], sys.argv[3])
        raise SystemExit(0)

    # Unterbefehl "modus": safe | assist | autonom. Ob das Hochstufen
    # gerade erlaubt ist (Kill-Switch), entscheidet der Job-Server -
    # hier wird nur geprueft, dass der Wert einer der drei Modi ist.
    if len(sys.argv) >= 2 and sys.argv[1] == "modus":
        if len(sys.argv) != 3:
            raise SystemExit("Aufruf: autonomie.py modus <safe|assist|autonom>")
        modus_setzen(sys.argv[2])
        raise SystemExit(0)

    # Unterbefehl "normal": alles zurueck auf den Auslieferungszustand.
    if len(sys.argv) >= 2 and sys.argv[1] == "normal":
        normalzustand_setzen()
        raise SystemExit(0)

    print(status_text())
    print()
    # Selbsttest 1: im Auslieferungszustand (safe, alles nein) muss ALLES
    # blockiert sein.
    #
    # Gegen eine FIXTURE, nicht gegen die laufende conf - das war bis zum
    # 29.08.2026 anders und ein echter Fehler: Sobald Mexla den Modus
    # legitim auf "autonom" stellte (was er am 29.08. tat), meldete der
    # Test FEHLER fuer jeden freigeschalteten Bereich. Nichts war kaputt,
    # der Test mass nur den Betriebszustand statt der Logik. Der
    # woechentliche Selbsttestlauf montags um 04:00 haette das ab da
    # jede Woche als Fehler gemeldet - und ein Fehler, der immer kommt,
    # wird ueberlesen.
    #
    # Es ist ausserdem die Hausregel, an der sich hier alles ausrichtet:
    # Selbsttests fassen keine Betriebsdaten an, auch nicht lesend.
    fehler = 0
    import tempfile as _tf1
    _probe1 = Path(_tf1.gettempdir()) / "m1_autonomie_auslieferung.conf"
    _probe1.write_text(
        "AUTONOMIE_MODUS=safe\n"
        + "".join("%s=nein\n" % s for s in SETZBARE_SCHALTER)
        + "".join("%s=ja\n" % s for s in HARTE_GRENZEN.values()),
        encoding="utf-8")
    _echt1 = list(CONF_KANDIDATEN)
    CONF_KANDIDATEN[:] = [_probe1]
    try:
        for bereich in list(BEREICH_SCHALTER) + list(HARTE_GRENZEN):
            erlaubt, _ = pruefe_aktion(bereich)
            if erlaubt:
                print(f"FEHLER: '{bereich}' waere im Auslieferungszustand "
                      "erlaubt!")
                fehler += 1
        # Gegenprobe, damit der Test nicht aus dem falschen Grund
        # besteht: Mit freigeschaltetem Schalter MUSS 'shell' durchgehen.
        # Ohne diese Haelfte bestuende der Test auch dann, wenn
        # pruefe_aktion() grundsaetzlich False lieferte.
        _probe1.write_text("AUTONOMIE_MODUS=autonom\nERLAUBE_SHELL=ja\n"
                           + "".join("%s=ja\n" % s
                                     for s in HARTE_GRENZEN.values()),
                           encoding="utf-8")
        if not pruefe_aktion("shell")[0]:
            print("FEHLER: 'shell' bleibt blockiert, obwohl autonom + "
                  "ERLAUBE_SHELL=ja - dann prueft der Test nichts!")
            fehler += 1
        # Und die harten Grenzen bleiben auch dann zu.
        for bereich in HARTE_GRENZEN:
            if pruefe_aktion(bereich)[0]:
                print(f"FEHLER: harte Grenze '{bereich}' im Modus autonom "
                      "offen!")
                fehler += 1
    finally:
        CONF_KANDIDATEN[:] = _echt1
        _probe1.unlink(missing_ok=True)
    if fehler == 0:
        print("Selbsttest OK: Auslieferungszustand blockiert alles, "
              "Freischaltung wirkt, harte Grenzen bleiben zu.")

    # Selbsttest 2: fehlende/unlesbare Config faellt auf den sichersten Fall zurueck.
    echte_kandidaten = list(CONF_KANDIDATEN)
    CONF_KANDIDATEN[:] = [Path("/gibt/es/nicht/autonomie.conf")]
    try:
        conf = lade_config()
        if conf.get("AUTONOMIE_MODUS") != "safe":
            print(f"FEHLER: ohne Config ist der Modus '{conf.get('AUTONOMIE_MODUS')}' statt 'safe'!")
            fehler += 1
        else:
            print("Selbsttest OK: fehlende autonomie.conf bedeutet safe/alles nein.")
    finally:
        CONF_KANDIDATEN[:] = echte_kandidaten

    # Selbsttest 3: harte Grenzen sind per conf NICHT abschaltbar.
    import tempfile
    echte_conf = list(CONF_KANDIDATEN)
    _tmp = Path(tempfile.gettempdir()) / "m1_autonomie_selbsttest.conf"
    # Die ERLAUBE_*-Zeilen MUESSEN mit hinein. Ohne sie blockiert auch die
    # alte, luetckenhafte Fassung noch - dann naemlich erst eine Zeile
    # spaeter an conf.get(schalter, "nein") != "ja". Der Test bestuende aus
    # dem falschen Grund und saehe eine Ruecknahme des Fixes nicht.
    # (In Runde 4 im Labor nachgestellt: mit der alten pruefe_aktion und
    #  ohne diese drei Zeilen meldete der Test faelschlich "OK".)
    _tmp.write_text(
        "AUTONOMIE_MODUS=autonom\n"
        "NIEMALS_KAEUFE=nein\n"
        "NIEMALS_NETZSPANNUNG=nein\n"
        "NIEMALS_LOESCHEN_OHNE_BACKUP=nein\n"
        "ERLAUBE_KAUF=ja\n"
        "ERLAUBE_NETZSPANNUNG=ja\n"
        "ERLAUBE_LOESCHEN=ja\n",
        encoding="utf-8")
    CONF_KANDIDATEN[:] = [_tmp]
    echte_schalter = dict(BEREICH_SCHALTER)
    try:
        # Zusaetzlich den Ausbau-Fall nachstellen: jemand traegt die drei
        # Bereiche spaeter in BEREICH_SCHALTER ein und schaltet sie in der
        # conf frei. Auch dann muss Schluss sein.
        BEREICH_SCHALTER["kauf"] = "ERLAUBE_KAUF"
        BEREICH_SCHALTER["netzspannung"] = "ERLAUBE_NETZSPANNUNG"
        BEREICH_SCHALTER["loeschen"] = "ERLAUBE_LOESCHEN"
        offen = [b for b in HARTE_GRENZEN if pruefe_aktion(b)[0]]
        if offen:
            print(f"FEHLER: harte Grenzen per conf abgeschaltet: {offen}")
            fehler += len(offen)
        else:
            print("Selbsttest OK: harte Grenzen lassen sich per conf nicht abschalten.")
    finally:
        BEREICH_SCHALTER.clear()
        BEREICH_SCHALTER.update(echte_schalter)
        CONF_KANDIDATEN[:] = echte_conf
        _tmp.unlink(missing_ok=True)

    # Selbsttest 4: der Gleichlauf-Test muss ANSCHLAGEN, wenn die
    # Kill-Switch-Pruefung nur noch als Kommentar existiert. Genau das
    # hat die Textsuche aus Runde 2 durchgewunken.
    _probe = Path(tempfile.gettempdir()) / "m1_gleichlauf_probe.sh"
    _pflicht_text = " ".join([
        "/opt/ki-server/STOP", "$HOME/Desktop/M1_DEPLOYMENT/STOP",
        "/Users/*/Desktop/M1_DEPLOYMENT/STOP", "/Volumes/M1_DEPLOYMENT/STOP",
        "/Volumes/Extreme SSD/M1_DEPLOYMENT/STOP",
        "/Volumes/SanDisk/M1_DEPLOYMENT/STOP",
        "/Volumes/SANDISK/M1_DEPLOYMENT/STOP"])
    _probe.write_text("#!/bin/bash\n# " + _pflicht_text + "\necho hallo\n",
                      encoding="utf-8")
    if _funktionsrumpf(_probe.read_text(encoding="utf-8"), "m1_stop_aktiv") is None:
        print("Selbsttest OK: eine Datei mit den Pfaden NUR im Kommentar "
              "gilt nicht mehr als geprueft.")
    else:
        print("FEHLER: Kommentar-Attrappe wird faelschlich als Pruefung gewertet!")
        fehler += 1
    _probe.unlink(missing_ok=True)

    # Selbsttest 5: Kill-Switch-Orte in Python und Shell muessen deckungsgleich sein.
    probleme = _pruefe_stop_gleichlauf()
    if probleme:
        print()
        print("FEHLER: Der Kill-Switch ist NICHT ueberall gleich definiert:")
        for pr in probleme:
            print(f"  - {pr}")
        fehler += len(probleme)
    elif _script_ordner() is None:
        print("Selbsttest uebersprungen: keine scripts/ gefunden "
              "(normal nach der Installation unter /opt/ki-server).")
    else:
        print(f"Selbsttest OK: alle Scripte in {_script_ordner()} pruefen "
              "dieselben STOP-Orte wie autonomie.py.")

    # Selbsttest 6: Schalter-Setzen ersetzt nur den Wert (Kommentar und
    # Ausrichtung bleiben) und die gesperrten Schluessel bleiben gesperrt.
    _probe_text = ("ERLAUBE_SHELL=nein   # Kommentar bleibt\n"
                   "NIEMALS_KAEUFE=ja\n")
    _neu = _wert_setzen(_probe_text, "ERLAUBE_SHELL", "ja")
    if ("ERLAUBE_SHELL=ja   # Kommentar bleibt" in _neu
            and "NIEMALS_KAEUFE=ja" in _neu):
        print("Selbsttest OK: Schalter-Setzen ersetzt nur den Wert.")
    else:
        print(f"FEHLER: Schalter-Setzen zerlegt die conf-Zeile: {_neu!r}")
        fehler += 1
    _gesperrt = set(HARTE_GRENZEN.values()) | {"AUTONOMIE_MODUS"}
    if _gesperrt & set(SETZBARE_SCHALTER):
        print("FEHLER: gesperrte Schluessel waeren per Tim setzbar: "
              f"{sorted(_gesperrt & set(SETZBARE_SCHALTER))}")
        fehler += 1
    else:
        print("Selbsttest OK: NIEMALS_* und AUTONOMIE_MODUS laufen nicht "
              "ueber den ja/nein-Schalter.")

    # Selbsttest 7: der Modus nimmt nur die drei bekannten Werte. Ohne
    # diese Pruefung landete ein Tippfehler ("assit") in der conf, und
    # lade_config() faende einen Modus, den pruefe_aktion nicht kennt.
    # Geprueft wird modus_pruefen(), NICHT modus_setzen() - sonst
    # schriebe der Selbsttest in die laufende conf (passiert am
    # 22.08.2026 genau so: der Modus stand danach auf "autonom").
    _modus_fehler = 0
    for _unsinn in ("ASSIST!", "root", "", "ja", "safe; rm -rf /"):
        try:
            modus_pruefen(_unsinn)
        except SystemExit:
            continue
        print(f"FEHLER: Modus '{_unsinn}' wurde angenommen!")
        _modus_fehler += 1
    for _gut, _erwartet in ((" Autonom ", "autonom"), ("SAFE", "safe"),
                            ("assist", "assist")):
        if modus_pruefen(_gut) != _erwartet:
            print(f"FEHLER: '{_gut}' wurde nicht zu '{_erwartet}' bereinigt!")
            _modus_fehler += 1
    if _modus_fehler:
        fehler += _modus_fehler
    else:
        print("Selbsttest OK: der Modus nimmt nur safe/assist/autonom.")

    # Selbsttest 8: der Normalzustand ist wirklich der sichere Zustand -
    # sonst waere der "zurueck auf sicher"-Knopf eine Attrappe.
    if (NORMALZUSTAND.get("AUTONOMIE_MODUS") == "safe"
            and all(NORMALZUSTAND.get(s) == "nein" for s in SETZBARE_SCHALTER)
            and set(SETZBARE_SCHALTER) <= set(NORMALZUSTAND)):
        print("Selbsttest OK: Normalzustand ist safe und alle Schalter nein.")
    else:
        print(f"FEHLER: Normalzustand ist nicht der sichere Zustand: {NORMALZUSTAND}")
        fehler += 1

    if fehler:
        raise SystemExit(1)
