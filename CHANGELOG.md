# Änderungen

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
