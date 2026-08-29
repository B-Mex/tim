#!/usr/bin/env python3
"""Handbuch-Vorschlag: was aus dem Lernprotokoll ins Handbuch gehoerte.

Warum es diese Datei gibt (29.08.2026): Tims Handbuch
(~/Desktop/Tim-Werkstatt/gelernt/HANDBUCH.md) ist die verdichtete
Fassung seines Lernprotokolls. ANHAENGEN kann Tim selbst
(werkstatt.py -> lernnotiz), VERDICHTEN bisher nicht - der Schritt
Tagebuch -> Handbuch war Handarbeit von Claude. Genau diese
Abhaengigkeit soll bis Ende November 2026 wegfallen.

Diese Datei macht den Schritt maschinell - aber NUR ALS VORSCHLAG:

  * Sie liest LERNPROTOKOLL.md und HANDBUCH.md, beide NUR LESEND.
  * Sie schreibt genau EINE Datei: berichte/handbuch_vorschlag.md.
  * HANDBUCH.md wird NIE geschrieben. Ein Handbuch, das sich selbst
    ueberschreiben kann, verliert genau das, wofuer es da ist: dass
    ein Mensch jede Regel darin einmal angesehen hat. Festgehalten
    wird das nicht von diesem Satz, sondern vom Schreib-Waechter im
    Selbsttest - und zur Laufzeit von der SHA256-Gegenprobe, die
    diese Datei vor und nach dem Lauf selbst zieht.
  * Die Messlatte steht im Kopf des Handbuchs: Eine Regel gehoert nur
    hinein, wenn sie GEMESSEN wurde; Vermutungen bleiben im
    Protokoll. Saetze mit Vermutungswoertern werden deshalb getrennt
    ausgewiesen und NICHT vorgeschlagen.

Woher der Vergleichspunkt kommt: Das Handbuch nennt seinen Stand im
Kopf ("Die Kurzfassung von 140 Lernnotizen ..."). Alles, was im
Protokoll danach steht, ist seit der letzten Verdichtung dazugekommen.

Die Auswahl ist eine VORSORTIERUNG nach Stichworten und Wortdeckung,
kein Urteil. Was eingetragen wird, entscheidet Mexla.

Aufruf:  handbuch_vorschlag.py              Vorschlag schreiben
         handbuch_vorschlag.py --selbsttest Pruefungen (nur Fixtures)
"""

import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

GELERNT = Path.home() / "Desktop" / "Tim-Werkstatt" / "gelernt"
HANDBUCH = GELERNT / "HANDBUCH.md"
PROTOKOLL = GELERNT / "LERNPROTOKOLL.md"
ZIEL = Path("/opt/ki-server/berichte/handbuch_vorschlag.md")

# Ein Satz schlaegt eine REGEL vor, wenn er wie eine Anweisung klingt.
# Kurze Allerweltssilben stehen hier bewusst NICHT drin: "statt" waere
# in jedem Satz mit dem Wort "Werkstatt" ein Treffer.
REGEL_MARKER = (
    "muss", "muessen", "darf nicht", "duerfen nicht", "niemals", "nie ",
    "immer ", "gilt ", "regel", "lektion", "wichtig", "gehoert",
    "gehoeren", "ab jetzt", "grundsatz", "merksatz", "man sollte",
)

# ... und er zaehlt nur, wenn im Satz ein BELEG steckt. "Nur gemessene
# Regeln gehoeren ins Handbuch" steht so im Handbuch-Kopf - hier ist es
# die Messlatte, an der ein Satz vorbeikommen muss.
MESSUNG_MARKER = (
    "gemessen", "nachgemessen", "getestet", "test", "selbsttest",
    "testfall", "testfaelle", "belegt", "beleg", "exitcode", "exit 1",
    "bewiesen", "nachgewiesen", "gegenprobe", "reproduziert", "gruen",
    "durchgefallen", "bestanden", "fehlgeschlagen",
)

# Vermutungen bleiben im Protokoll. Dieser Riegel steht VOR dem
# Messungs-Riegel: Ein Satz, der beides traegt ("ich habe getestet,
# vermutlich liegt es an..."), ist eine Vermutung mit Beiwerk und
# gehoert nicht ins Handbuch. Die vorsichtige Richtung ist die richtige.
VERMUTUNG_MARKER = (
    "vermutlich", "vermute", "wahrscheinlich", "vielleicht", "koennte",
    "duerfte", "ich denke", "ich glaube", "scheint", "moeglicherweise",
    "eventuell", "anscheinend", "offenbar", "womoeglich", "gefuehl",
)

# Ab dieser Wortdeckung gilt eine Regel als im Handbuch bereits
# vorhanden. 0.4 heisst: zwei von fuenf bedeutungstragenden Woertern
# des Satzes stehen schon in einem Kapitel.
# Warum 0.4 und nicht 0.5 (29.08.2026 am echten Protokoll gemessen):
# Bei 0.5 blieben drei Saetze im Vorschlag, die Kapitel 1 nur mit
# anderen Worten wiederholten (44 %, 44 %, 46 %). Verloren geht dabei
# nichts - der Satz steht dann im Abschnitt "Steht schon im Handbuch"
# und ist weiter lesbar, nur nicht mehr in der Arbeitsliste.
DECKUNGSGRENZE = 0.4

# Woerter, die ueberall vorkommen und deshalb nichts ueber Deckung
# aussagen. Kurze Woerter fallen ohnehin durch die Laengengrenze.
FUELLWOERTER = {
    "wichtig", "gelernt", "aufgabe", "lektion", "erkenntnis", "naechste",
    "naechstes", "selbst", "wirklich", "deshalb", "ausserdem",
    "allerdings", "trotzdem", "zunaechst", "zuerst", "muessen", "sollte",
    "koennen", "werden", "wurden", "haben", "damit", "wenn", "weil",
    "geworden", "gemacht", "machen", "richtig", "falsch", "immer",
    "niemals", "eigentlich", "einfach", "sondern", "beispiel",
}
WORT_MINDESTLAENGE = 5

# Ein Satz unter dieser Laenge ist Ueberschrift oder Aufzaehlungsrest,
# kein Regelsatz.
SATZ_MINDESTLAENGE = 40
SATZ_ANZEIGE = 400
HOECHSTENS_VORSCHLAEGE = 25


# ----------------------------------------------------------------------
# Lesen und Zerlegen (alles rein: Text rein, Daten raus)
# ----------------------------------------------------------------------
def normalisieren(text: str) -> str:
    """Kleinschreibung und Umlaute ausgeschrieben - Tims Notizen
    schwanken zwischen 'gruen' und 'grün', beides meint dasselbe."""
    text = text.lower()
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(alt, neu)
    return text


def worte(text: str) -> set[str]:
    """Bedeutungstragende Woerter eines Textes."""
    roh = re.findall(r"[a-z0-9_]+", normalisieren(text))
    return {w for w in roh
            if len(w) >= WORT_MINDESTLAENGE and w not in FUELLWOERTER}


def stand_lesen(handbuch_text: str) -> dict:
    """Was sagt der Handbuch-Kopf ueber seinen eigenen Stand?

    Erwartet wird die Zeile 'Die Kurzfassung von N Lernnotizen
    (LERNPROTOKOLL.md, TT.-TT.MM.JJJJ)'. Die Zahl ist der
    Vergleichspunkt: alles danach im Protokoll ist neu.
    """
    kopf = handbuch_text.split("\n## ", 1)[0]
    anzahl = re.search(r"(\d+)\s+Lernnotiz", kopf)
    bis = re.search(r"\d{1,2}\.\s*[-–]\s*(\d{1,2}\.\d{1,2}\.\d{4})", kopf)
    return {"notizen": int(anzahl.group(1)) if anzahl else None,
            "bis": bis.group(1) if bis else None}


def kapitel_lesen(handbuch_text: str) -> list[dict]:
    """Kapitel samt Stichworten (die Klammer hinter dem Titel)."""
    kapitel: list[dict] = []
    aktuell: dict | None = None
    for zeile in handbuch_text.splitlines():
        if zeile.startswith("## "):
            titel = zeile[3:].strip()
            klammer = re.search(r"\(([^)]*)\)\s*$", titel)
            stichworte = []
            if klammer:
                stichworte = [normalisieren(s.strip())
                              for s in klammer.group(1).split(",")
                              if s.strip()]
                titel = titel[:klammer.start()].strip()
            aktuell = {"titel": titel, "stichworte": stichworte, "text": ""}
            kapitel.append(aktuell)
        elif aktuell is not None:
            aktuell["text"] += zeile + "\n"
    for k in kapitel:
        k["worte"] = worte(k["text"])
    return kapitel


NOTIZ_KOPF = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) - (.+)$")


def notizen_lesen(protokoll_text: str) -> list[dict]:
    """Die Lernnotizen in ihrer Reihenfolge (aelteste zuerst)."""
    notizen: list[dict] = []
    aktuell: dict | None = None
    for zeile in protokoll_text.splitlines():
        passt = NOTIZ_KOPF.match(zeile)
        if passt:
            aktuell = {"datum": passt.group(1), "zeit": passt.group(2),
                       "aufgabe": passt.group(3).strip(), "text": ""}
            notizen.append(aktuell)
        elif aktuell is not None:
            aktuell["text"] += zeile + "\n"
    return notizen


def saetze(text: str) -> list[str]:
    """Text in Saetze zerlegen - zeilenweise, damit Aufzaehlungen
    ('1. Pruefen ob ...') nicht am Punkt der Nummer zerfallen.

    Am Doppelpunkt wird NICHT getrennt (29.08.2026 gemessen): Tim
    schreibt seine Belege davor und die Regel dahinter ("Ich habe im
    Selbsttest gemessen: die Wartezeit zaehlt nicht mit"). Wer dort
    trennt, reisst den Beleg von der Regel und wirft beide Haelften weg.
    """
    ergebnis = []
    for zeile in text.splitlines():
        zeile = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", zeile).strip()
        if not zeile:
            continue
        for satz in re.split(r"(?<=[.!?])\s+", zeile):
            satz = satz.strip()
            if len(satz) >= SATZ_MINDESTLAENGE:
                ergebnis.append(satz)
    return ergebnis


# ----------------------------------------------------------------------
# Beurteilen
# ----------------------------------------------------------------------
def _traeger(satz: str, marker: tuple) -> str | None:
    """Welches Markerwort traegt der Satz? (None = keines)

    Gesucht wird am WORTANFANG, nicht irgendwo im Wort (29.08.2026
    gemessen): Die reine Teilstringsuche hielt "Begruendung" fuer einen
    Beleg, weil darin "gruen" steckt. Ein Beleg, der aus einem
    Wortinneren stammt, ist keiner. Endungen bleiben erlaubt -
    "beleg" soll "belegt" finden.
    """
    klein = normalisieren(satz)
    for m in marker:
        if re.search(r"\b" + re.escape(m), klein):
            return m.strip()
    return None


def ist_regel(satz: str) -> bool:
    return _traeger(satz, REGEL_MARKER) is not None


def vermutungswort(satz: str) -> str | None:
    return _traeger(satz, VERMUTUNG_MARKER)


def messungswort(satz: str) -> str | None:
    return _traeger(satz, MESSUNG_MARKER)


def deckung(satz: str, kapitel: list[dict]) -> tuple[str | None, float]:
    """Wie stark steht der Satz schon im Handbuch? (Kapitel, Anteil)"""
    eigen = worte(satz)
    if not eigen:
        return None, 0.0
    bestes, beste_quote = None, 0.0
    for k in kapitel:
        quote = len(eigen & k["worte"]) / len(eigen)
        if quote > beste_quote:
            bestes, beste_quote = k["titel"], quote
    return bestes, beste_quote


def kapitel_vorschlagen(satz: str, aufgabe: str,
                        kapitel: list[dict]) -> tuple[str | None, float]:
    """In welches Kapitel gehoerte die Regel? Stichwort schlaegt Wortdeckung."""
    text = normalisieren(satz + " " + aufgabe)
    eigen = worte(satz)
    bestes, beste_punkte = None, 0.0
    for k in kapitel:
        punkte = 3.0 * sum(1 for s in k["stichworte"] if s and s in text)
        if eigen:
            punkte += 2.0 * len(eigen & k["worte"]) / len(eigen)
        if punkte > beste_punkte:
            bestes, beste_punkte = k["titel"], punkte
    return bestes, beste_punkte


def beurteilen(neue_notizen: list[dict],
               kapitel: list[dict]) -> dict:
    """Jeden Regelsatz der neuen Notizen einsortieren.

    Drei Koerbe: vorschlag (gemessen und neu), protokoll (Vermutung
    oder ohne Beleg - bleibt, wo es ist), schon_da (steht bereits).
    """
    vorschlag, protokoll, schon_da = [], [], []
    for notiz in neue_notizen:
        for satz in saetze(notiz["text"]):
            if not ist_regel(satz):
                continue
            eintrag = {"notiz": notiz, "satz": satz}
            wort = vermutungswort(satz)
            if wort:
                eintrag["grund"] = f"Vermutungswort '{wort}'"
                protokoll.append(eintrag)
                continue
            beleg = messungswort(satz)
            if not beleg:
                # Der Beleg muss im SATZ stehen - sonst wuerde ein
                # einziges "Selbsttest" irgendwo in der Notiz jeden
                # Satz daneben zur gemessenen Regel adeln. Wo die Notiz
                # aber einen Beleg traegt, wird das gesagt: dann lohnt
                # das Nachlesen, und Mexla sieht, wo er hinschauen muss.
                in_notiz = messungswort(notiz["text"])
                eintrag["grund"] = (
                    f"kein Beleg im Satz - die Notiz nennt '{in_notiz}'"
                    if in_notiz else
                    "kein Beleg im Satz und keiner in der Notiz")
                protokoll.append(eintrag)
                continue
            eintrag["beleg"] = beleg
            titel, quote = deckung(satz, kapitel)
            eintrag["deckung"] = quote
            eintrag["deckung_kapitel"] = titel
            if quote >= DECKUNGSGRENZE:
                schon_da.append(eintrag)
                continue
            ziel, punkte = kapitel_vorschlagen(satz, notiz["aufgabe"], kapitel)
            eintrag["kapitel"] = ziel
            eintrag["kapitel_punkte"] = punkte
            vorschlag.append(eintrag)
    return {"vorschlag": vorschlag, "protokoll": protokoll,
            "schon_da": schon_da}


# ----------------------------------------------------------------------
# Berichten
# ----------------------------------------------------------------------
def _kurz(satz: str) -> str:
    return satz if len(satz) <= SATZ_ANZEIGE else satz[:SATZ_ANZEIGE] + " ..."


def _quelle(notiz: dict) -> str:
    return f"{notiz['datum']} {notiz['zeit']} - {notiz['aufgabe']}"


def bericht_bauen(stand: dict, notizen: list[dict], neu: list[dict],
                  koerbe: dict, sha_vorher: str, sha_nachher: str,
                  stempel: str) -> str:
    z = []
    z.append(f"# Handbuch-Vorschlag - {stempel}\n")
    z.append("Das ist ein VORSCHLAG, keine Aenderung. HANDBUCH.md wurde "
             "nur gelesen; eintragen bleibt Handarbeit von Mexla.\n")
    z.append("**Beleg dafuer:** SHA256 von HANDBUCH.md\n")
    z.append(f"- vorher:  `{sha_vorher}`")
    z.append(f"- nachher: `{sha_nachher}`")
    z.append(f"- {'unveraendert' if sha_vorher == sha_nachher else 'ABWEICHUNG - siehe unten'}\n")

    z.append("## Stand\n")
    z.append(f"- Handbuch-Kopf nennt: {stand['notizen']} Lernnotizen"
             + (f", verdichtet bis {stand['bis']}" if stand["bis"] else ""))
    z.append(f"- Lernprotokoll enthaelt jetzt: {len(notizen)} Lernnotizen")
    z.append(f"- Seit der letzten Verdichtung dazugekommen: {len(neu)}")
    if neu:
        z.append(f"- Davon die neueste: {_quelle(neu[-1])}")
    z.append("")

    if not neu:
        z.append("Nichts zu tun: Das Handbuch ist auf dem Stand des "
                 "Protokolls. Der naechste Lauf sieht wieder nach.\n")
    else:
        z.append("## Vorschlag: gemessene Regeln, die noch fehlen\n")
        vor = koerbe["vorschlag"]
        if not vor:
            z.append("Keine. In den neuen Notizen steht keine gemessene "
                     "Regel, die nicht schon im Handbuch steht.\n")
        else:
            z.append(f"{len(vor)} Satz/Saetze. Vor dem Eintragen jeden "
                     "einzeln pruefen: Ist die Regel wirklich gemessen, "
                     "und ist sie allgemein genug fuers Handbuch?\n")
            for nummer, e in enumerate(vor[:HOECHSTENS_VORSCHLAEGE], 1):
                ziel = e["kapitel"] or "(kein Kapitel getroffen - Mexla entscheidet)"
                z.append(f"### {nummer}. {ziel}\n")
                z.append(f"Quelle: {_quelle(e['notiz'])}\n")
                z.append("> " + _kurz(e["satz"]) + "\n")
                z.append(f"Beleg im Satz: \"{e['beleg']}\" - "
                         f"Wortdeckung mit dem Handbuch: "
                         f"{e['deckung'] * 100:.0f} %"
                         + (f" (naechstes Kapitel: {e['deckung_kapitel']})"
                            if e["deckung_kapitel"] else "") + "\n")
            if len(vor) > HOECHSTENS_VORSCHLAEGE:
                z.append(f"... und {len(vor) - HOECHSTENS_VORSCHLAEGE} "
                         "weitere. Erst diese hier abarbeiten.\n")

        z.append("## Bleibt im Protokoll\n")
        rest = koerbe["protokoll"]
        if not rest:
            z.append("Nichts.\n")
        else:
            z.append(f"{len(rest)} Satz/Saetze klingen nach Regel, tragen "
                     "aber ein Vermutungswort oder keinen Beleg. Das "
                     "Handbuch nimmt nur Gemessenes. Wo der Grund lautet "
                     "\"die Notiz nennt ...\", steckt der Beleg im "
                     "Umfeld: Da lohnt das Nachlesen im Protokoll, bevor "
                     "die Regel verworfen wird.\n")
            for e in rest[:HOECHSTENS_VORSCHLAEGE]:
                z.append(f"- ({e['grund']}) {_quelle(e['notiz'])}: "
                         + _kurz(e["satz"]))
            if len(rest) > HOECHSTENS_VORSCHLAEGE:
                z.append(f"- ... und {len(rest) - HOECHSTENS_VORSCHLAEGE} weitere")
            z.append("")

        z.append("## Steht schon im Handbuch\n")
        alt = koerbe["schon_da"]
        if not alt:
            z.append("Nichts.\n")
        else:
            z.append(f"{len(alt)} Satz/Saetze wiederholen, was schon "
                     "drinsteht - kein Handlungsbedarf.\n")
            for e in alt[:HOECHSTENS_VORSCHLAEGE]:
                z.append(f"- {e['deckung_kapitel']} "
                         f"({e['deckung'] * 100:.0f} % Deckung): "
                         + _kurz(e["satz"]))
            if len(alt) > HOECHSTENS_VORSCHLAEGE:
                z.append(f"- ... und {len(alt) - HOECHSTENS_VORSCHLAEGE} weitere")
            z.append("")

    z.append("## Wie diese Liste entstanden ist\n")
    z.append("Maschinell, in dieser Reihenfolge: (1) Notizen ab Nummer "
             f"{stand['notizen']} gelten als neu - so weit reicht der "
             "Handbuch-Kopf. (2) Ein Satz klingt nach Regel, wenn er ein "
             "Anweisungswort traegt. (3) Ein Vermutungswort schliesst ihn "
             "aus, auch wenn daneben ein Beleg steht. (4) Ohne Beleg einer "
             "Messung bleibt er ebenfalls im Protokoll. (5) Deckt sich "
             f"mehr als {DECKUNGSGRENZE * 100:.0f} % seiner Woerter mit "
             "einem Kapitel, steht er schon da. (6) Das Kapitel wird nach "
             "den Stichworten hinter dem Kapiteltitel geraten.\n")
    z.append("Das ist eine Vorsortierung, kein Urteil. Sie kann einen "
             "guten Satz uebersehen und einen schlechten vorschlagen - "
             "deshalb entscheidet ein Mensch, was eingetragen wird.\n")
    return "\n".join(z)


def _sha(pfad: Path) -> str:
    try:
        return hashlib.sha256(pfad.read_bytes()).hexdigest()
    except OSError:
        return "(nicht lesbar)"


def vorschlag_schreiben(handbuch: Path = HANDBUCH,
                        protokoll: Path = PROTOKOLL,
                        ziel: Path = ZIEL,
                        jetzt: str | None = None) -> tuple[int, str]:
    """Vorschlag bauen und ablegen. Rueckgabe: (exitcode, Zusammenfassung).

    Exit 1 nur bei echten Stoerungen (Datei fehlt, Kopf unlesbar,
    Protokoll kuerzer als der Handbuch-Stand). Ein Vorschlag ist der
    NORMALFALL und macht deshalb keinen Laerm - die Wochenroutine
    schlaegt sonst jede Woche Alarm, und ein Alarm, der immer kommt,
    ist keiner (dieselbe Lehre wie beim kamera-plist-Hinweis).
    """
    stempel = jetzt or datetime.now().strftime("%d.%m.%Y %H:%M")
    for pfad, name in ((handbuch, "HANDBUCH.md"), (protokoll, "LERNPROTOKOLL.md")):
        if not pfad.is_file():
            return 1, f"FEHLER: {name} liegt nicht unter {pfad}."

    sha_vorher = _sha(handbuch)
    handbuch_text = handbuch.read_text(encoding="utf-8", errors="replace")
    protokoll_text = protokoll.read_text(encoding="utf-8", errors="replace")

    stand = stand_lesen(handbuch_text)
    if stand["notizen"] is None:
        return 1, ("FEHLER: Der Handbuch-Kopf nennt keine Zahl von "
                   "Lernnotizen - ohne diesen Stand gibt es keinen "
                   "Vergleichspunkt.")
    notizen = notizen_lesen(protokoll_text)
    if len(notizen) < stand["notizen"]:
        return 1, (f"FEHLER: Das Handbuch nennt {stand['notizen']} Notizen, "
                   f"das Protokoll hat nur {len(notizen)}. Einer von beiden "
                   "Staenden stimmt nicht.")

    neu = notizen[stand["notizen"]:]
    kapitel = kapitel_lesen(handbuch_text)
    koerbe = beurteilen(neu, kapitel)
    sha_nachher = _sha(handbuch)

    text = bericht_bauen(stand, notizen, neu, koerbe,
                         sha_vorher, sha_nachher, stempel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(text, encoding="utf-8")

    if sha_vorher != sha_nachher:
        # Kann nur passieren, wenn jemand anders waehrend des Laufs
        # geschrieben hat - oder wenn diese Datei kaputtrepariert wurde.
        # Beides gehoert laut gemeldet.
        return 1, (f"FEHLER: HANDBUCH.md hat sich waehrend des Laufs "
                   f"geaendert ({sha_vorher[:12]} -> {sha_nachher[:12]}).")

    zusammenfassung = (
        f"Handbuch-Vorschlag liegt in {ziel}.\n"
        f"Handbuch-Kopf nennt {stand['notizen']} Notizen"
        + (f" (bis {stand['bis']})" if stand["bis"] else "") + ".\n"
        f"Lernprotokoll hat jetzt {len(notizen)} Notizen"
        f" - {len(neu)} neu seit der Verdichtung.\n"
        f"Regelsaetze darin: {len(koerbe['vorschlag'])} vorgeschlagen, "
        f"{len(koerbe['protokoll'])} bleiben im Protokoll, "
        f"{len(koerbe['schon_da'])} stehen schon im Handbuch.\n"
        f"HANDBUCH.md wurde nur gelesen - SHA256 unveraendert "
        f"({sha_vorher[:12]}).")
    return 0, zusammenfassung


# ====================== Selbsttest ==========================
# Kein Test fasst Betriebsdaten an - weder HANDBUCH.md noch
# LERNPROTOKOLL.md noch berichte/. Alles laeuft gegen Fixtures in
# einem Temp-Ordner, und ein Schreib-Waechter haelt fest, WELCHE Pfade
# ueberhaupt geoeffnet wurden. Damit wird die Kernlinie dieser Datei
# ("schreibt das Handbuch nicht") gemessen statt behauptet.

def _selbsttest() -> int:
    import builtins
    import io
    import pathlib
    import tempfile

    fehler = [0]

    def pruefe(bedingung, text, zusatz=""):
        stand = "ok     " if bedingung else "FEHLER "
        print(f"  {stand} {text}"
              + (f"  [{zusatz}]" if zusatz and not bedingung else ""))
        if not bedingung:
            fehler[0] += 1

    print("handbuch_vorschlag.py Selbsttest:")

    # --- Der Schreib-Waechter: jeder Dateizugriff wird mitgeschrieben ---
    zugriffe: list[tuple[str, str]] = []
    echte_open, echte_io_open = builtins.open, io.open
    echte_read, echte_write = pathlib.Path.read_text, pathlib.Path.write_text
    echte_bytes = pathlib.Path.read_bytes

    def merken(pfad, modus):
        zugriffe.append((str(pfad), str(modus)))

    def open_wacht(datei, modus="r", *a, **k):
        merken(datei, modus)
        return echte_open(datei, modus, *a, **k)

    def io_open_wacht(datei, modus="r", *a, **k):
        merken(datei, modus)
        return echte_io_open(datei, modus, *a, **k)

    def read_wacht(self, *a, **k):
        merken(self, "r")
        return echte_read(self, *a, **k)

    def bytes_wacht(self, *a, **k):
        merken(self, "rb")
        return echte_bytes(self, *a, **k)

    def write_wacht(self, *a, **k):
        merken(self, "w")
        return echte_write(self, *a, **k)

    handbuch_probe = (
        "# Tims Handbuch\n\n"
        "Die Kurzfassung von 3 Lernnotizen (LERNPROTOKOLL.md, "
        "24.-26.08.2026), sortiert nach Fachgebieten.\n\n"
        "Wer hier etwas aendert: Eine Regel gehoert nur ins Handbuch, "
        "wenn sie GEMESSEN wurde.\n\n"
        "## KERN - gilt immer\n\n"
        "1. **Nie behaupten, was ich nicht gemessen habe.**\n\n"
        "## Kapitel 1 - Pruefen und Testen (test, selbsttest, pruefung, luecke)\n\n"
        "- Drei Zustaende immer getrennt zaehlen: bestanden, "
        "durchgefallen, uebersprungen. Nie verrechnen.\n\n"
        "## Kapitel 3 - Das Haus: Lampen, Funk (lampe, shelly, funk, pico, mesh)\n\n"
        "- Pico-Hochlauf dauert ungefaehr zehn Sekunden.\n\n"
        "## Kapitel 7 - Zeit und Grenzen (zeitgrenze, warteschlange, timeout)\n\n"
        "- Exakt auf der Grenze zaehlt als nicht abgelaufen.\n")

    protokoll_probe = (
        "# Tims Lernprotokoll\n\n"
        "## 2026-08-24 05:07 - pfad_riegel\n\n"
        "Alte Notiz eins, laengst verdichtet und ohne neue Aussage.\n\n"
        "## 2026-08-25 06:00 - pfad_riegel\n\n"
        "Alte Notiz zwei, ebenfalls schon im Handbuch angekommen.\n\n"
        "## 2026-08-26 07:00 - zeitgrenze\n\n"
        "Alte Notiz drei, die dritte und letzte verdichtete.\n\n"
        "## 2026-08-27 08:00 - zeitgrenze\n\n"
        "Ich habe im Selbsttest gemessen: Die Wartezeit in der "
        "Warteschlange darf NICHT mitgezaehlt werden, sonst gelten "
        "schnelle Anfragen faelschlich als abgelaufen.\n\n"
        "## 2026-08-27 09:00 - lampen_zeitplan\n\n"
        "Vermutlich muss man die Sequenznummer bei 255 umbrechen, "
        "getestet habe ich das aber noch nicht zu Ende.\n\n"
        "## 2026-08-27 10:00 - luecke_statt_ok\n\n"
        "Drei Zustaende immer getrennt zaehlen: bestanden, "
        "durchgefallen, uebersprungen - im Testfall belegt, nie "
        "verrechnen.\n\n"
        "## 2026-08-27 11:00 - allerlei\n\n"
        "Man sollte die Ausgabe huebscher formatieren, das sieht "
        "einfach besser aus und liest sich angenehmer als bisher.\n\n"
        "## 2026-08-27 12:00 - doppelablage\n\n"
        "Im Selbsttest habe ich beide Ordner miteinander verglichen.\n"
        "Der Vergleich muss ueber den Inhalt laufen, nicht ueber die "
        "Aenderungszeit der Datei.\n")

    tmp = Path(tempfile.mkdtemp(prefix="m1_handbuch_test_"))
    try:
        fix_handbuch = tmp / "HANDBUCH.md"
        fix_protokoll = tmp / "LERNPROTOKOLL.md"
        fix_ziel = tmp / "berichte" / "handbuch_vorschlag.md"
        fix_handbuch.write_text(handbuch_probe, encoding="utf-8")
        fix_protokoll.write_text(protokoll_probe, encoding="utf-8")
        sha_vor = hashlib.sha256(fix_handbuch.read_bytes()).hexdigest()

        # --- Kopf, Kapitel, Notizen lesen ---
        stand = stand_lesen(handbuch_probe)
        pruefe(stand["notizen"] == 3,
               "Handbuch-Kopf nennt seinen Stand (3 Notizen)", str(stand))
        pruefe(stand["bis"] == "26.08.2026",
               "und bis wann verdichtet wurde", str(stand))
        pruefe(stand_lesen("# Ohne Stand\n\nNichts.\n")["notizen"] is None,
               "fehlender Stand wird als fehlend gemeldet, nicht geraten")

        kapitel = kapitel_lesen(handbuch_probe)
        pruefe([k["titel"] for k in kapitel][:1] == ["KERN - gilt immer"],
               "KERN wird als Kapitel gelesen",
               str([k["titel"] for k in kapitel]))
        k1 = [k for k in kapitel if k["titel"].startswith("Kapitel 1")]
        pruefe(bool(k1) and "selbsttest" in k1[0]["stichworte"],
               "Stichworte kommen aus der Klammer hinter dem Titel",
               str(k1[0]["stichworte"]) if k1 else "kein Kapitel 1")
        pruefe(bool(k1) and "(" not in k1[0]["titel"],
               "die Klammer steht nicht mehr im Titel")

        notizen = notizen_lesen(protokoll_probe)
        pruefe(len(notizen) == 8, "alle Notizen gefunden", str(len(notizen)))
        pruefe(notizen[0]["datum"] == "2026-08-24"
               and notizen[-1]["aufgabe"] == "doppelablage",
               "aelteste zuerst, juengste zuletzt")

        # --- Der Vergleichspunkt: was ist NEU? (beide Seiten) ---
        neu = notizen[stand["notizen"]:]
        pruefe(len(neu) == 5, "fuenf Notizen sind neu seit der Verdichtung",
               str(len(neu)))
        pruefe(all(n["datum"] >= "2026-08-27" for n in neu),
               "und es sind wirklich die spaeteren")
        # Gegenprobe: ohne den Stand aus dem Kopf waeren ALLE neu -
        # der Vergleichspunkt ist also die tragende Regel, nicht Beiwerk.
        pruefe(len(notizen[0:]) == 8 and len(neu) < len(notizen),
               "ohne Stand waeren es alle acht - der Kopf traegt")

        # --- Die Messlatte: gemessen ja, Vermutung nein ---
        koerbe = beurteilen(neu, kapitel)
        vor_saetze = " | ".join(e["satz"] for e in koerbe["vorschlag"])
        prot_saetze = " | ".join(e["satz"] for e in koerbe["protokoll"])
        alt_saetze = " | ".join(e["satz"] for e in koerbe["schon_da"])
        pruefe("Warteschlange" in vor_saetze,
               "gemessene neue Regel wird vorgeschlagen", vor_saetze[:90])
        pruefe("Vermutlich" not in vor_saetze and "Vermutlich" in prot_saetze,
               "Vermutung bleibt im Protokoll, auch mit Regelwort",
               prot_saetze[:90])
        pruefe("huebscher" not in vor_saetze and "huebscher" in prot_saetze,
               "Regelsatz ohne Beleg bleibt ebenfalls im Protokoll")

        # Ein Marker gilt nur am Wortanfang - "Begruendung" ist kein
        # Beleg, auch wenn "gruen" darin steckt. Beide Seiten:
        pruefe(messungswort("Die Begruendung dazu war unvollstaendig.") is None,
               "'Begruendung' ist kein Beleg (Marker nur am Wortanfang)",
               str(messungswort("Die Begruendung dazu war unvollstaendig.")))
        pruefe(messungswort("Der Selbsttest lief gruen durch.") is not None,
               "ein echtes 'gruen' wird weiterhin gefunden")

        # Steht der Beleg nicht im Satz, aber in der Notiz, sagt der
        # Grund das - sonst wirft Mexla eine gute Regel weg.
        gruende = " | ".join(e["grund"] for e in koerbe["protokoll"])
        pruefe("die Notiz nennt" in gruende,
               "ein Beleg im Umfeld wird benannt, nicht verschwiegen",
               gruende[:120])
        pruefe("Drei Zustaende" in alt_saetze
               and "Drei Zustaende" not in vor_saetze,
               "was schon im Handbuch steht, wird nicht nochmal "
               "vorgeschlagen", alt_saetze[:90])

        # Gegenprobe zur Deckungsgrenze: mit Grenze 1.0 (nichts gilt als
        # gedeckt) muss derselbe Satz wieder im Vorschlag landen.
        global DECKUNGSGRENZE
        echte_grenze = DECKUNGSGRENZE
        try:
            DECKUNGSGRENZE = 1.01
            k2 = beurteilen(neu, kapitel)
            pruefe(any("Drei Zustaende" in e["satz"] for e in k2["vorschlag"]),
                   "ohne Deckungsgrenze faellt der Doppelte durch - die "
                   "Grenze wirkt also wirklich")
        finally:
            DECKUNGSGRENZE = echte_grenze

        # --- Kapitelzuordnung nach Stichwort ---
        ziel_kapitel = [e["kapitel"] for e in koerbe["vorschlag"]
                        if "Warteschlange" in e["satz"]]
        pruefe(ziel_kapitel and ziel_kapitel[0] is not None
               and ziel_kapitel[0].startswith("Kapitel 7"),
               "die Warteschlangen-Regel landet in Kapitel 7 "
               "(Stichwort 'warteschlange')", str(ziel_kapitel))
        pruefe(kapitel_vorschlagen("Der Selbsttest muss gruen sein.",
                                   "irgendwas", []) == (None, 0.0),
               "ohne passendes Kapitel wird keines erfunden")
        titel, punkte = kapitel_vorschlagen(
            "Der Selbsttest muss die Luecke getrennt zaehlen.",
            "luecke_statt_ok", kapitel)
        pruefe(titel is not None and titel.startswith("Kapitel 1"),
               "Stichwort 'selbsttest' fuehrt zu Kapitel 1", str(titel))
        titel2, _ = kapitel_vorschlagen(
            "Der Pico am Mesh muss nach dem Funk neu starten.",
            "bruecken_sequenz", kapitel)
        pruefe(titel2 is not None and titel2.startswith("Kapitel 3"),
               "Stichworte 'pico/mesh/funk' fuehren zu Kapitel 3", str(titel2))

        # --- Der ganze Lauf, mit Schreib-Waechter ---
        builtins.open = open_wacht
        io.open = io_open_wacht
        pathlib.Path.read_text = read_wacht
        pathlib.Path.read_bytes = bytes_wacht
        pathlib.Path.write_text = write_wacht
        try:
            rc, text = vorschlag_schreiben(fix_handbuch, fix_protokoll,
                                           fix_ziel, jetzt="01.01.2000 00:00")
        finally:
            builtins.open, io.open = echte_open, echte_io_open
            pathlib.Path.read_text = echte_read
            pathlib.Path.read_bytes = echte_bytes
            pathlib.Path.write_text = echte_write

        pruefe(rc == 0, "der Lauf endet gruen", text[:120])
        pruefe(fix_ziel.is_file(), "der Vorschlag liegt am vereinbarten Ort")

        # Ein Schreibvorgang taucht doppelt auf (Path.write_text ruft
        # intern io.open) - der Waechter horcht mit Absicht an beiden
        # Stellen. Fuer die Frage "welche Dateien" zaehlt jeder Pfad einmal.
        geschrieben = list(dict.fromkeys(
            p for p, m in zugriffe if "w" in m or "a" in m or "+" in m))
        pruefe(str(fix_handbuch) not in geschrieben,
               "HANDBUCH.md wurde NICHT geschrieben - die Kernlinie",
               str(geschrieben))
        pruefe(str(fix_protokoll) not in geschrieben,
               "LERNPROTOKOLL.md wurde NICHT geschrieben")
        pruefe(geschrieben == [str(fix_ziel)],
               "genau eine Datei wurde geschrieben: der Vorschlag",
               str(geschrieben))
        pruefe(hashlib.sha256(fix_handbuch.read_bytes()).hexdigest() == sha_vor,
               "und der SHA256 des Handbuchs ist derselbe wie vorher")

        # Kein Zugriff auf die ECHTEN Dateien - Hausregel 3.
        echte = [str(HANDBUCH), str(PROTOKOLL), str(ZIEL)]
        beruehrt = [p for p, _ in zugriffe if p in echte]
        pruefe(not beruehrt,
               "der Selbsttest fasst keine Betriebsdaten an", str(beruehrt))

        inhalt = fix_ziel.read_text(encoding="utf-8")
        pruefe("VORSCHLAG" in inhalt and "Handarbeit von Mexla" in inhalt,
               "der Bericht sagt selbst, dass er nur ein Vorschlag ist")
        pruefe(sha_vor in inhalt,
               "der Bericht traegt den SHA256-Beleg des Handbuchs")
        pruefe("Warteschlange" in inhalt,
               "die vorgeschlagene Regel steht im Bericht")

        # --- Die Zusammenfassung darf die Wochenroutine nicht faelschlich
        # alarmieren. PROBLEM_MARKER kommt aus routine.py selbst, damit
        # beide Listen nicht auseinanderlaufen.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from routine import PROBLEM_MARKER
            treffer = [m for m in PROBLEM_MARKER if m in text]
            pruefe(not treffer,
                   "die gruene Zusammenfassung enthaelt keinen "
                   "Problem-Marker der Routine", str(treffer))
        except ImportError as f:
            pruefe(False, "routine.py fuer den Marker-Abgleich ladbar", str(f))

        # --- Stoerungen sind laut: beide Seiten ---
        rc, text = vorschlag_schreiben(tmp / "gibtsnicht.md", fix_protokoll,
                                       fix_ziel)
        pruefe(rc == 1 and "FEHLER" in text,
               "fehlendes Handbuch: Exit 1 und laut", text[:80])
        ohne_stand = tmp / "ohne_stand.md"
        ohne_stand.write_text("# Handbuch\n\nOhne Zahl im Kopf.\n",
                              encoding="utf-8")
        rc, text = vorschlag_schreiben(ohne_stand, fix_protokoll, fix_ziel)
        pruefe(rc == 1 and "FEHLER" in text,
               "unlesbarer Stand: Exit 1 und laut", text[:80])
        kurz = tmp / "kurz.md"
        kurz.write_text("# Handbuch\n\nDie Kurzfassung von 99 Lernnotizen "
                        "(LERNPROTOKOLL.md, 24.-26.08.2026).\n",
                        encoding="utf-8")
        rc, text = vorschlag_schreiben(kurz, fix_protokoll, fix_ziel)
        pruefe(rc == 1 and "FEHLER" in text,
               "Protokoll kuerzer als der Handbuch-Stand: Exit 1 und laut",
               text[:80])

        # --- Nichts Neues ist kein Fehler ---
        voll = tmp / "voll.md"
        voll.write_text(handbuch_probe.replace("von 3 Lernnotizen",
                                               "von 8 Lernnotizen"),
                        encoding="utf-8")
        rc, text = vorschlag_schreiben(voll, fix_protokoll, fix_ziel)
        pruefe(rc == 0 and "0 neu" in text,
               "Handbuch auf Stand: Exit 0, kein Laerm", text[:120])
        pruefe("Nichts zu tun" in fix_ziel.read_text(encoding="utf-8"),
               "und der Bericht sagt es in Worten")

        # --- Satzzerlegung: Aufzaehlungen zerfallen nicht am Punkt ---
        s = saetze("1. Der Selbsttest muss die Luecke getrennt zaehlen.\n"
                   "- Kurz\n")
        pruefe(any(x.startswith("Der Selbsttest") for x in s),
               "Nummerierung wird abgestreift, der Satz bleibt ganz", str(s))
        pruefe(all(len(x) >= SATZ_MINDESTLAENGE for x in s),
               "Bruchstuecke fallen raus", str(s))
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    print("\n" + (f"ROT: {fehler[0]} Fehler" if fehler[0]
                  else "Alle Pruefungen gruen."))
    return 1 if fehler[0] else 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        raise SystemExit(_selbsttest())
    _rc, _text = vorschlag_schreiben()
    print(_text)
    raise SystemExit(_rc)
