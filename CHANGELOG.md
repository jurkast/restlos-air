# Änderungen

## 1.6.3 – 2026-10-22

- Leiste übersichtlicher: Werte in Gruppen (Temperaturen | Speicher | Top | Kühlung | Netz | Audio) mit dünnen Trennlinien.
- Kurze Überschrift + fetter Wert statt Sätzen: „PUMPE 2.323 U/min“, „PING 21 ms · 65 %“, „TOP claude-desktop 1 %“. Details stehen im Tooltip.
- Feste Feldbreiten: nichts wird mehr zu „…“ abgeschnitten, und die Leiste verändert ihre Breite nicht mehr mit den Werten.

## 1.6.2 – 2026-10-22

- Fehler: Mit „Lautstärke und Mikrofon“ in der Leiste startete das Audio-Fenster sich beim Starten immer wieder selbst (Endlosschleife). Dadurch blieben Widget und Leiste ohne Werte. Das Audio wird jetzt genau einmal gestartet.

## 1.6.1 – 2026-10-22

- Fehler: Löste eine Zusatzfunktion einmal eine Ausnahme aus, hielt GTK die sekündliche Messung an und Widget/Leiste zeigten keine Werte mehr. Die Messung läuft jetzt in jedem Fall weiter; Fehler landen in `~/.local/state/restlos-air/fehler.log`.

## 1.6.0 – 2026-10-22

- **Wer frisst?** Größter Verbraucher in Widget und Leiste, im Studio die Top 5 nach CPU-Last mit RAM und „Beenden“ (nur eigene Programme, Desktop und Audio sind geschützt).
- **Lüfterprofile** Leise, Normal und Spiel für alle regelbaren Lüfter per Klick in Widget, Leiste oder Studio; jedes erreicht 100 % spätestens bei 85 °C.
- **Spielmodus automatisch:** Steam-, Lutris- und Heroic-Spiele werden erkannt, das Profil wechselt auf „Spiel“ und eine Minute nach Spielende zurück.
- **Verlauf:** Spitzenwerte des Tages mit Uhrzeit und laufendem Spiel/Programm, 24-Stunden-Diagramm; sieben Tage lokal gespeichert (~2000 Zeilen).
- **Staubhinweis:** steigt die Leerlauftemperatur über Wochen um 6 °C, schlägt Air vor, Kühler und Filter zu prüfen.
- **Mainboard, Takt & Laufwerke:** Mainboard-Sensoren, CPU- und GPU-Takt, GPU-Leistung in Watt, Lesen/Schreiben der Laufwerke.
- **Mini-Verlaufslinien** der letzten Minute in den Temperaturkacheln.
- **Leiste zusammenstellen:** Werte per Häkchen wählen und mit ↑/↓ sortieren; neu wählbar: Festplatten, GPU-Watt, CPU-Takt, Wer frisst, Profil, Audio.
- **Audio in der Leiste:** Klick schaltet den Ton, Mausrad ändert die Lautstärke, Mikrofon per Klick stumm.
- **Heller Modus** (Dunkel, Hell oder wie das System), Farben wie im Uninstaller.
- **Tastenkürzel Super+Umschalt+A** blendet Widget oder Leiste ein/aus (wird in den Zorin-/GNOME-Einstellungen angelegt); dazu `restlos-air --toggle`.

## 1.5.2 – 2026-10-22

- Leiste bleibt fest an der gewählten Kante: Die Seitenleiste wird höchstens breiter, nie schmaler, und rückt bei wechselnden Werten nicht mehr hin und her.
- Gewählte Kante und Monitor bleiben nach Neustart und Update erhalten; der Monitor wird über Modell und Position erkannt, auch bei zwei gleichen Bildschirmen.

## 1.5.1 – 2026-10-22

- Leiste oben/unten läuft jetzt über die ganze Monitorbreite. Vorher wurde der Arbeitsbereich des Monitors verwendet, der auf X11 beim zweiten Monitor oft nur einen Teil abdeckt; dadurch fehlten auch Pumpe, Ping und Datenrate („…“).
- Leiste rechts/links: Breite wird gemessen statt fest angenommen; sie ragt nicht mehr über den Bildschirmrand hinaus.
- Platz freihalten nur noch an äußeren Kanten. Grenzt dort ein anderer Monitor an, hätte die Reservierung den ganzen Nachbarmonitor blockiert; das Studio zeigt jetzt einen Hinweis.

## 1.5.0 – 2026-10-22

- Neue Ansicht **Leiste**: Restlos Air als Taskleiste oben, unten, links oder rechts am gewählten Bildschirm.
- Zeigt CPU/GPU mit Last, Hotspot, SSD, RAM/VRAM-Balken, Pumpe, Ping, Datenrate, Status und UPDATE.
- „Platz freihalten“ reserviert den Bereich, damit maximierte Fenster nicht darüber liegen (X11).
- Umschalten im Studio unter „Warnungen & Widget → Ansicht“; Tray-Menü blendet Widget oder Leiste ein/aus.

## 1.4.0 – 2026-10-22

- Automatische, signierte Updates wie bei Restlos Bewerbung und Uninstaller: stündliche Prüfung,
  Ed25519-Signatur, SHA-256, Installation ohne Rootrechte, sofortiger Rückfall bei Startfehlern.
- **UPDATE** im Widget und „Jetzt neu starten und aktualisieren“ im Studio, Benachrichtigung mit Neustart-Knopf.
- Neuer Bereich „Updates“ im Studio mit Version, Status, „Jetzt prüfen“ und Schalter für die stündliche Prüfung.
- Release-Workflow: Jede Programmänderung auf `main` erzeugt automatisch ein signiertes Release.

## 1.3.0 – 2026-10-22

- Zahnrad ⚙ im Widget öffnet die Einstellungen (Klick auf die Werte weiterhin auch).
- Neuer Bereich „Warnungen & Widget“ im Studio: Warn- und Alarmgrenzen für CPU, GPU, Hotspot und SSD einstellbar, gespeichert, mit „Standardwerte“.
- Ungültige Grenzwerte (Warnung ≥ Alarm) werden abgelehnt.

## 1.2.0 – 2026-10-22

- Widget neu gestaltet: 300 statt 390 px breit, Design wie Restlos Bewerbung und Uninstaller.
- Vier Temperaturkacheln (CPU, GPU, Hotspot, SSD) mit Farbstufen, Lastbalken für CPU, GPU, RAM und VRAM.
- Statusplakette LIVE / WARM / HEISS / DATEN FEHLEN; Benachrichtigung nennt die Werte und wird bei Entwarnung zurückgezogen.
- NVMe-SSD-Temperatur; bei mehreren SSDs zählt die heißeste.
- „Im Vordergrund“ wird gespeichert und gilt für Widget, Studio und Audio.
- Fehler: Überhitzung wurde bei ausgeblendetem Widget nicht gemeldet.
- Fehler: Alarm flatterte um 85 °C; jetzt 5 °C Hysterese.
- Fehler: Bei APU plus Grafikkarte konnte die falsche GPU gelesen werden; die dedizierte Karte hat Vorrang.
- Fehler: Fehlermeldungen verwiesen auf „Zurück zur Automatik“, die Schaltfläche heißt „Vorherige Regelung“.
- Fehler: Platzhaltertext beim Pumpenkanal und fester Grafikkartenname ersetzt.
- Zahlen mit deutschem Dezimalkomma; Sensorsuche nur noch alle 30 Sekunden statt jede Sekunde.

## 1.1.1 – 2026-10-13

- WLAN-Prozentwert eindeutig als „WLAN-Signal“ beschriftet.

## 1.1.0 – 2026-10-13

- CPU-/GPU-Auslastung sowie RAM- und Grafikspeicherverbrauch in Widget und Studio.
- Aktive WLAN-Signalstärke ohne Scan sowie Empfangs- und Senderate.
- Eigenes rahmenloses Audio-Widget im selben Prozess, mit Lautstärke, Ausgabe- und Mikrofon-Stummschaltung.
- Gerätewahl für Ausgabe und Mikrofon; laufende Audioverbindungen werden übernommen, Monitor-Aufnahmen bleiben erhalten.
- Audio reagiert auf Dienstereignisse; Regleränderungen werden gebündelt und auf 0–100 % begrenzt.
- Position und Sichtbarkeit des Audio-Widgets werden gespeichert; Zugriff über das vorhandene Tray-Menü.
- Tests für fehlende Geräte, Audiofehler, Interfacewechsel und Zählerreset.

## 1.0.1 – 2026-10-13

Erste öffentliche GitHub-Version.

- Rahmenloses Desktop-Widget mit Tray-Symbol und Start beim Anmelden.
- Air Studio per Klick mit Temperaturen, Drehzahlen und Lüfterkennlinien.
- NCT6798-Kanäle 1–3 über CoolerControl steuerbar; Kanal 6 und GPU als Anzeigen.
- Gesicherte Wiederherstellung und Temperaturschutz für manuelle Vorgaben.
- WLAN-/LAN-Ping zum Gateway und zum Internetziel, mit ICMP-Paketverlust.
- Pumpendrehzahl aus dem vorhandenen Dienstcache; keine direkten NCT-Fanabfragen im Widget.
- Hardwarefreie Tests und reproduzierbarer Debian-Paketbau.
- MIT-Lizenz.
