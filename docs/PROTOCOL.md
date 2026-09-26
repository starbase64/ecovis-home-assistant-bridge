# ECO-WORTHY / ECOVIS 2400 – Protokoll-Referenz

Reverse-engineered am Modell **ECO-BPS2400WDZ** (App-Kennung `BW1E_B48E`,
Gerätemodell `BHY2.5-1600`, Firmware `V1.1.1.7`). Alle Werte wurden gegen die
Anzeige der offiziellen ECO-WORTHY-App verifiziert.

> Adressen und konkrete Beispiel-Telegramme stammen von einem einzelnen Gerät.
> Die interne Protokolladresse und einige Konfig-Bytes (z. B. im 0x2032-Block)
> sind gerätespezifisch – für das eigene Gerät mit den Tools in `tools/`
> ermitteln.

## Transport

Die App steuert den Wechselrichter **nicht lokal**, sondern über einen
Cloud-WebSocket:

```
wss://app.eco-worthy.com/ws/websocket
```

Nachrichten sind JSON: `{"mac": "<WLAN-MAC>", "msg": "<Hex-Telegramm>"}`.
Der Server begrüßt neue Verbindungen mit dem Klartext `连接成功`
(„Verbindung erfolgreich"). Ein sichtbarer Login/Token wird **nicht** benötigt.

**Wichtig:** Die Cloud erlaubt praktisch nur **eine aktive Sitzung pro Gerät**.
Ist die offizielle App verbunden, wird eine zweite Verbindung (z. B. die Bridge)
serverseitig geschlossen (WebSocket-Code 1000).

## Telemetrie abrufen

```
1. senden:  {"mac": MAC, "msg": "FF10070187240014ffffF6D6"}   (Init)
2. Gerät:   {"mac": MAC, "msg": "ff10004c30"}                 (ACK)
3. senden:  {"mac": MAC, "msg": ""}                           (leerer Abruf)
4. Gerät streamt die Blöcke  f77f  02  03  04  05  06  07  08  09
5. für frische Werte: leeren Abruf "" erneut senden
```

Adresse endet auf `44`, die sichtbare WLAN-MAC auf `43` – diese Abweichung
ist beabsichtigt und darf in Telegrammen nicht „korrigiert" werden.

## Telegramm-Aufbau (Schreibbefehle)

Modbus-ähnlich mit CRC16-Modbus (Low-Byte zuerst auf dem Draht):

```
<6B interne Adresse> <Func 0x10> <Register 2B> <Anzahl 2B> <Länge 1B> <Nutzdaten> <CRC16-LE 2B>
```

Die Schreib-Bestätigung (ACK) ist: `<Adresse> 10 <Register> <Anzahl> <CRC16-LE>`.

## Block `f77f` – Live-Betriebswerte

16-bit big-endian, Offset in Byte ab Blockanfang:

| Offset | Feld | Skala | Einheit |
|-------:|------|-------|---------|
| 24 | PV1+2 Spannung | ×0,01 | V |
| 26 | PV1+2 Strom | ×0,01 | A |
| 28 | PV3+4 Spannung | ×0,01 | V |
| 30 | PV3+4 Strom | ×0,01 | A |
| 40 | Netzspannung | ×0,1 | V |
| 42 | Netzfrequenz | ×1 | Hz |
| 46 | AC-Leistung (vorzeichenbehaftet, − = Entladen) | ×1 | W |
| 48 | AC-Leistung (Betrag) | ×1 | W |
| 52 | Leistungsfaktor | ×0,01 | – |
| 54 | Innentemperatur | ×1 | °C |
| 58 | Batteriespannung | ×0,01 | V |
| 60 | Batteriestrom (vorzeichenbehaftet, − = Entladen) | ×0,01 | A |
| 62 | SOC | ×1 | % |
| 64 | SOH | ×1 | % |
| 66 | Batteriekapazität | ×1 | Wh |
| 68 | Batterietemperatur | ×1 | °C |

Abgeleitet:
- **PV-Leistung** = PV1+2 (V·I) + PV3+4 (V·I)
- **Batterieleistung** = Batteriespannung · Batteriestrom
- **Restkapazität** = SOC/100 · Batteriekapazität

Blockkopf `02` enthält als ASCII die **Seriennummer** und **Firmware-Version**.

## Block `09` – Energie- und Erlöszähler

16-bit big-endian:

| Offset | Feld | Skala | Einheit |
|-------:|------|-------|---------|
| 28 | Erzeugung heute | ×0,01 | kWh |
| 34 | Gesamtertrag | ×0,01 | kWh |
| 38 | Gesamtverbrauch | ×0,01 | kWh |
| 40 | Erlös heute | ×0,01 | € |
| 44 | Gesamterlös | ×0,01 | € |

## Register `0x2031` – Rückstromeinspeisung EIN/AUS

Ein Register schreiben (Func 0x10, Anzahl 1, Wert 0/1):

```
EIN:  <addr>102031000102 0001 <crc>
AUS:  <addr>102031000102 0000 <crc>
```

## Register `0x2032` – Betriebsmodus, Einspeise- und Ladeleistung

Ein Block aus 10 Registern (18 Byte Nutzdaten). Alle Felder verifiziert:

| Nutzdaten-Byte | Feld |
|---------------:|------|
| 0–1 | Modus: `0x0001` Zeitplan / `0x0000` Automatik |
| 2–3 | Entlade-/Einspeise-Sollwert (W, ×1) |
| 4–14 | fester Block: `027f 028a 0640 0001 7f00 8a` (u. a. Entladegrenze 0x0640 = 1600 W; gerätespezifisch/Zeitplan-Konfig) |
| 15–16 | Ladeleistung (W, ×1) |
| 17 | Laden aktiv: `0x01` / `0x00` |

Beispiele (Adresse gerätespezifisch):

```
Zeitplan, Einspeisen 222 W:  ...102032000a12 0001 00de 027f028a064000017f008a 0321 00 <crc>
Zeitplan, Laden 333 W:       ...102032000a12 0001 0000 027f028a064000017f008a 014d 01 <crc>
Automatik:                   ...102032000a12 0000 ....  ...                        .. .. <crc>
```

Es sollte immer nur **eins** aktiv sein (Laden **oder** Entladen). Die Bridge
erzwingt das: ein Einspeise-Sollwert schaltet das Laden ab und umgekehrt.

## Register `0x218f` – Uhrzeit setzen

Nutzdaten `JJ MM TT HH MM SS`, jedes Byte als Dezimalwert
(z. B. `1a 09 19 0f 31 12` = 2026-09-25 15:49:18).

## BMS (separates Gerät)

Das Batterie-Pack (`ECO-LFP48100-3U-09D3F0`) ist ein **eigenes** Gerät mit
eigener MAC. Es spricht **nicht** mit der Cloud, sondern lokal über
**Bluetooth LE** (JBD/Jiabaida-artiges Protokoll). Die App loggt die BLE-Frames;
16 Zellspannungen (mV, 16-bit big-endian nach dem Marker `0010` = 16 Zellen),
Pack-Spannung (×0,01 V) und 4 Temperaturen ((roh−500)/10 °C) sind dekodierbar.
Eine HA-Anbindung dafür (z. B. ESP32/ESPHome als BLE-Proxy) ist noch nicht Teil
dieses Projekts.
