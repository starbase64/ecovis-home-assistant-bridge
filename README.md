# Ecovis Home Assistant Bridge

Home-Assistant-Anbindung für das **ECO-WORTHY / ECOVIS 2400** Balkonkraftwerk
(Balcony PowerStation, Modell `ECO-BPS2400WDZ` / `BW1E_B48E` / `BHY2.5-1600`).

Der Wechselrichter bietet **keine lokale API** – die App steuert ihn über einen
ECO-WORTHY-Cloud-WebSocket. Dieses Projekt spricht dasselbe Protokoll: Es liest
alle Betriebswerte aus, legt sie per **MQTT-Discovery** automatisch in Home
Assistant an und erlaubt die **direkte Steuerung** von Einspeise-/Ladeleistung
und Betriebsmodus. Alle Telegramme wurden gegen die offizielle App verifiziert.

> ⚠️ Inoffiziell und ohne Gewähr. Reverse-engineered am eigenen Gerät im eigenen
> Netz. Schreibbefehle greifen real ins Gerät ein – auf eigene Verantwortung.
> Kein Bezug zu oder Unterstützung durch ECO-WORTHY.

## Funktionen

**Sensoren (lesen)**
- PV gesamt sowie je Strang (PV1+2, PV3+4): Spannung, Strom, Leistung
- AC-Leistung, Netzspannung, Netzfrequenz, Leistungsfaktor, Innentemperatur
- Batterie: Spannung, Strom, Leistung, Temperatur, SOC, SOH, Kapazität, Restkapazität
- Energiezähler: Erzeugung heute, Gesamtertrag, Gesamtverbrauch, Erlös heute, Gesamterlös

**Steuerung (schreiben)**
- `select` **Betriebsmodus**: Automatik (Smart-Meter/CT) oder Zeitplan (manuell)
- `number` **Einspeiseleistung** (Entladen, 0–800 W, konfigurierbar)
- `number` **Ladeleistung** (0 = aus)
- `switch` **Rückstromeinspeisung** EIN/AUS
- Laden und Entladen schließen sich gegenseitig aus (immer nur eins aktiv)

## Wie es funktioniert

```
ECOVIS 2400  <--WLAN-->  ECO-WORTHY-Cloud  <--WebSocket-->  Bridge (Docker)
                                                                 |
                                                              MQTT
                                                                 |
                                                          Home Assistant
```

Die Bridge verbindet sich mit dem Cloud-WebSocket, ruft alle 60 s die Telemetrie
ab (Blöcke `f77f` + `09`), dekodiert sie und veröffentlicht die Werte per MQTT.
Steuerbefehle schreibt sie als Modbus-ähnliche Telegramme (CRC16-Modbus) und
prüft die Bestätigung. Details in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

**Wichtige Einschränkung:** Die Cloud erlaubt nur **eine Sitzung pro Gerät**.
Solange die offizielle App verbunden ist, kann die Bridge nicht lesen/schreiben
(sie meldet dann `degraded` und versucht es beim nächsten Intervall erneut).
Für Dauerbetrieb in HA also die App geschlossen halten.

## Installation

Voraussetzungen: Docker + Docker Compose, ein MQTT-Broker (z. B. Mosquitto) und
die MQTT-Integration in Home Assistant.

```bash
git clone https://github.com/starbase64/ecovis-home-assistant-bridge.git
cd ecovis-home-assistant-bridge
cp .env.example .env
nano .env            # MAC und MQTT-Zugang eintragen (siehe unten)
docker compose up -d --build
docker compose logs -f
```

Erfolgreich sieht der Log etwa so aus:

```
Discovery veroeffentlicht (25 Sensoren + Schalter/Button).
Telemetrie ok: PV=653W AC=1358W SOC=79%
```

### Konfiguration (`.env`)

| Variable | Bedeutung |
|----------|-----------|
| `ECOVIS_MAC` | WLAN-MAC des Geräts, ohne Doppelpunkte (App → Einstellungen → Geräteinformationen) |
| `ECOVIS_INTERNAL_ADDR` | interne Protokolladresse = MAC, **letztes Byte +1** (z. B. `…5b43` → `…5b44`) |
| `MQTT_HOST` / `MQTT_PORT` | Adresse deines MQTT-Brokers |
| `MQTT_USER` / `MQTT_PASS` | MQTT-Zugang (leer lassen für anonym) |
| `ECOVIS_TELEMETRY_INTERVAL` | Poll-Intervall in Sekunden (Standard 60; nicht zu klein wegen Rate-Limit) |
| `ECOVIS_MAX_WATTS` | harte Obergrenze für Ein-/Ladeleistung (Standard 800) |

Die `.env` ist per `.gitignore` ausgenommen – deine MAC/Zugangsdaten landen
nicht im Repo.

## Home-Assistant-Dashboard

Eine fertige Lovelace-Ansicht liegt in
[`homeassistant/dashboard.yaml`](homeassistant/dashboard.yaml). Einbinden über
Dashboard → Bearbeiten → 3-Punkte → Rohkonfigurations-Editor. Die
`sensor.ecovis_*` / `number.ecovis_*` / `select.ecovis_*` Entitäten entstehen
automatisch per Discovery. (Die Eco-Tracker-Zeilen im Dashboard sind Platzhalter
für ein optionales Smart-Meter und können angepasst oder entfernt werden.)

## Tools (eigenes Gerät reversen)

Adresse und einige Konfig-Bytes sind gerätespezifisch. Mit den Skripten in
[`tools/`](tools/) ermittelst du sie für dein eigenes Gerät:

- `ecovis_capture.py` – nimmt einen vollständigen Telemetrie-Mitschnitt auf
- `ecovis_decode.py` – findet Byte-Offsets per Kreuzkorrelation über mehrere Mitschnitte
- `adb_extract.py` – zieht Telemetrie-Blöcke aus einem Android-`logcat` (ADB)

Prinzip: In der App einen Wert ablesen/ändern und gleichzeitig das Telegramm bzw.
die Telemetrie mitschneiden, dann den Byte-Offset bestimmen. Siehe
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Sicherheit

- Schreibbefehle sind echte Eingriffe. Mit kleinen Werten testen.
- `ECOVIS_MAX_WATTS` begrenzt die Sollwerte hart.
- Jeder Schreibbefehl wird gegen die erwartete Geräte-ACK geprüft.
- Ein Smart-Meter-basierter Regelkreis (z. B. Shelly-Simulator) ist als
  Rückfallebene sinnvoll, bis die direkte Steuerung erprobt ist.

## Status

- ✅ Wechselrichter: alle Betriebswerte + Energiezähler gelesen, Steuerung vollständig
- ⏳ BMS (Batterie-Pack): läuft über Bluetooth LE (JBD-artig), Protokoll dekodiert;
  HA-Anbindung (z. B. ESP32/ESPHome als BLE-Proxy) noch offen

## Lizenz

MIT – siehe [LICENSE](LICENSE).
