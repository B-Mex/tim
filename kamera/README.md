# Tims Auge — Kameradienst

Webcam-Dienst mit offener Objekterkennung (YOLOE): erkennt frei wählbare
Begriffe statt fester Klassen. Läuft als eigener Dienst auf Port 8781 und
wird von der Zentrale ausschließlich über HTTP angesprochen.

## Lizenz: AGPL-3.0 — abweichend vom restlichen Projekt

**Dieser Ordner steht unter der GNU AGPL-3.0** ([LICENSE](LICENSE)), nicht
unter der MIT-Lizenz des übrigen Repos.

Der Grund: `objekterkennung.py` und `kamera_dienst.py` binden
[Ultralytics](https://github.com/ultralytics/ultralytics) ein, und
Ultralytics steht unter AGPL-3.0. Diese Lizenz ist „ansteckend“ (Copyleft):
Programme, die sie einbinden, müssen unter denselben Bedingungen weiter-
gegeben werden. Auch die verwendeten YOLO-/YOLOE-Modellgewichte stammen von
Ultralytics.

Was das praktisch bedeutet:

* **Privat nutzen, ändern, ausprobieren:** völlig frei, keine Auflagen.
* **Weitergeben oder als Netzdienst anbieten:** dann muss der Quelltext
  dieses Ordners (samt eigener Änderungen) mitgeliefert bzw. den Nutzern
  zugänglich gemacht werden. Genau das ist hier ohnehin der Fall.
* **In ein geschlossenes Produkt einbauen:** geht nicht — dafür bräuchte es
  eine kommerzielle Ultralytics-Lizenz.

Der Rest des Projekts (Zentrale, Job-Server, Harness, Sprachassistent) ist
davon **nicht** betroffen: Er bindet diesen Code nicht ein, sondern spricht
über HTTP mit ihm und bleibt MIT-lizenziert.

Wer den ganzen Baum unter MIT halten will, müsste Erkennung **und** Modell
durch eine permissiv lizenzierte Lösung ersetzen — die Bibliothek allein
auszutauschen genügt nicht, solange die Gewichte von Ultralytics stammen.

## Betrieb

Der Dienst wird über `launchagents/com.ki-server.kamera.plist` gestartet.
Beim ersten Start fragt macOS nach der Kamera-Freigabe für Python; einmal
erlauben, danach läuft er ohne Nachfrage. Nicht mitgeliefert werden die
Modelldateien (`tim_auge.pt`, `yolo26n.pt`) — sie entstehen bzw. werden
lokal abgelegt, siehe `objekterkennung.py`.

## Mehrere Messfelder und die Raum-Zuordnung (seit 31.08.2026)

Das Auge kann mehrere Leuchten **einzeln** messen. Vorher sah es zwar
mehrere Lampen, hatte aber nur ein Messfeld — und damit war die Frage
„welche Lampe gehört zum Büro, welche zum Flur?" nicht zu beantworten.

* `kamera_dienst.py` hält jetzt eine Liste `messfelder`. `messfeld` ist
  weiterhin da und **ist** das erste Feld (dasselbe Objekt): Alles, was
  nur ein Messfeld kennt — `kamera_cli.py`, der Sprachassistent, die
  Zentrale, `/messfeld` — arbeitet unverändert weiter. `/messung` liefert
  die gewohnten Werte fürs Hauptfeld und zusätzlich `felder` mit einer
  Messung je Leuchte. `messfeld.json` wird als Liste geschrieben, das
  alte Einzelobjekt aber weiterhin gelesen.
* `zuordnung.py` ist die Auswertung: Was ändert sich, wenn ein Raum
  geschaltet wird? Die Regeln stammen von **Tim selbst** (Werkstatt-
  Aufgabe `lampen_zuordnen`, 24.08.2026); neu sind die Auswertung je Feld
  und die Farbe als zweiter Hinweis neben der Helligkeit.
* `zuordnungslauf.py` führt das Experiment durch: Raum schalten, vorher
  und nachher jedes Feld messen, auswerten. Es schaltet **echtes Licht**
  und tut deshalb ohne `--schalten` gar nichts.

Der Kameradienst schaltet dabei selbst **nichts** — er misst nur. Das
Schalten übernimmt `lampen_steuern.py` über den Zuordnungslauf, damit
Kamera und Funkbrücke nicht aneinandergekettet sind.
