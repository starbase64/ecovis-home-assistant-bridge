#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecovis_ha_bridge.py - MQTT-Bridge fuer den ECOVIS 2400 Wechselrichter
                      (ECO-WORTHY ECO-BPS2400WDZ / BW1E_B48E) mit
                      Home Assistant MQTT-Discovery.

Liest den Wechselrichter ueber den ECO-WORTHY-Cloud-WebSocket:
  Init FF10070187240014ffff  ->  ACK  ->  leerer Abruf ""  ->  Bloecke f77f + 02..09
Dekodiert die Betriebswerte aus f77f und die Energiezaehler aus Block 09
(alle Offsets an echten Geraetedaten verifiziert) und legt daraus HA-Sensoren an.
Zusaetzlich: Schalter fuer die Rueckstromeinspeisung (Register 0x2031).

Hinweis: Die Cloud erlaubt praktisch nur EINE Sitzung pro Geraet. Ist die
ECO-WORTHY-App verbunden, wird die Bridge abgewiesen -> sie meldet dann
'degraded' und versucht es beim naechsten Intervall ruhig erneut (kein Hammern).

Abhaengigkeiten:  pip install "websockets>=10.4" "aiomqtt>=2.0"
"""

import asyncio
import json
import logging
import os
import ssl
import sys

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    import aiomqtt
except ImportError as e:
    sys.exit(f"Fehlende Abhaengigkeit ({e}). "
             f"pip install \"websockets>=10.4\" \"aiomqtt>=2.0\"")

def _env(k, d=None, c=str):
    v = os.environ.get(k, d)
    return c(v) if v is not None else None

WS_URL        = _env("ECOVIS_WS_URL", "wss://app.eco-worthy.com/ws/websocket")
DEVICE_MAC    = _env("ECOVIS_MAC", "AABBCCDDEEFF")
INTERNAL_ADDR = _env("ECOVIS_INTERNAL_ADDR", "aabbccddeeff")
INIT_QUERY    = "FF10070187240014ffffF6D6"
INIT_ACK      = "ff10004c30"

MQTT_HOST = _env("MQTT_HOST", "127.0.0.1")
MQTT_PORT = _env("MQTT_PORT", "1883", int)
MQTT_USER = _env("MQTT_USER", None)
MQTT_PASS = _env("MQTT_PASS", None)

DISCOVERY_PREFIX   = _env("MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/")
NODE_ID            = _env("ECOVIS_NODE_ID", "ecovis")
CONNECT_TIMEOUT    = _env("ECOVIS_CONNECT_TIMEOUT", "10", float)
GREETING_TIMEOUT   = _env("ECOVIS_GREETING_TIMEOUT", "4", float)
COLLECT_SECONDS    = _env("ECOVIS_COLLECT_SECONDS", "6", float)
TELEMETRY_INTERVAL = _env("ECOVIS_TELEMETRY_INTERVAL", "60", int)
MAX_WATTS          = _env("ECOVIS_MAX_WATTS", "800", int)

BASE         = NODE_ID
T_AVAIL      = f"{BASE}/status"
T_FEED_SET   = f"{BASE}/reverse_feed/set"
T_FEED_STATE = f"{BASE}/reverse_feed/state"
T_TELE_REFR  = f"{BASE}/telemetry/refresh"
T_CONN_STATE = f"{BASE}/connection/state"
T_SET_CMD    = f"{BASE}/setpoint/set"
T_SET_STATE  = f"{BASE}/setpoint/state"
T_MODE_CMD   = f"{BASE}/mode/set"
T_MODE_STATE = f"{BASE}/mode/state"
T_CHG_CMD    = f"{BASE}/charge/set"
T_CHG_STATE  = f"{BASE}/charge/state"
def T_SENSOR(sid): return f"{BASE}/sensor/{sid}"

DEVICE_INFO = {
    "identifiers": [f"ecovis_bps2400_{DEVICE_MAC}"],
    "name": "ECOVIS 2400", "manufacturer": "ECO-WORTHY",
    "model": "ECO-BPS2400WDZ", "sw_version": "V1.1.1.7",
}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("ecovis-bridge")

# ------------------------------------------------------------------ #
#  Sensor-Definitionen: id, Name, Einheit, device_class, state_class, icon
#  Die id ist zugleich der HA-object_id -> Entitaet: sensor.ecovis_<id>
# ------------------------------------------------------------------ #
M, TOT, TI = "measurement", "total", "total_increasing"
SENSORS = [
 ("pv_power",          "PV-Leistung",            "W",   "power",       M,  "mdi:solar-power"),
 ("pv1_2_voltage",     "PV1+2 Spannung",         "V",   "voltage",     M,  None),
 ("pv1_2_current",     "PV1+2 Strom",            "A",   "current",     M,  None),
 ("pv1_2_power",       "PV1+2 Leistung",         "W",   "power",       M,  None),
 ("pv3_4_voltage",     "PV3+4 Spannung",         "V",   "voltage",     M,  None),
 ("pv3_4_current",     "PV3+4 Strom",            "A",   "current",     M,  None),
 ("pv3_4_power",       "PV3+4 Leistung",         "W",   "power",       M,  None),
 ("ac_power",          "AC-Leistung",            "W",   "power",       M,  "mdi:flash"),
 ("grid_voltage",      "Netzspannung",           "V",   "voltage",     M,  None),
 ("grid_frequency",    "Netzfrequenz",           "Hz",  "frequency",   M,  None),
 ("power_factor",      "Leistungsfaktor",        None,  "power_factor",M,  None),
 ("inverter_temp",     "Innentemperatur",        "°C",  "temperature", M,  None),
 ("battery_voltage",   "Batteriespannung",       "V",   "voltage",     M,  None),
 ("battery_current",   "Batteriestrom",          "A",   "current",     M,  None),
 ("battery_power",     "Batterieleistung",       "W",   "power",       M,  "mdi:battery-charging"),
 ("battery_temp",      "Batterietemperatur",     "°C",  "temperature", M,  None),
 ("soc",               "SOC",                    "%",   "battery",     M,  None),
 ("soh",               "SOH",                    "%",   None,          M,  "mdi:battery-heart-variant"),
 ("battery_capacity",  "Batteriekapazität",      "Wh",  None,          None,"mdi:battery"),
 ("battery_remaining", "Restkapazität",          "Wh",  None,          M,  "mdi:battery-70"),
 ("energy_today",      "Erzeugung heute",        "kWh", "energy",      TI, None),
 ("energy_total",      "Gesamtertrag",           "kWh", "energy",      TI, None),
 ("consumption_total", "Gesamtverbrauch",        "kWh", "energy",      TI, None),
 ("revenue_today",     "Erlös heute",            "EUR", "monetary",    TOT,"mdi:cash"),
 ("revenue_total",     "Gesamterlös",            "EUR", "monetary",    TOT,"mdi:cash-multiple"),
]

# ------------------------------------------------------------------ #
#  CRC / Telegramm (fuer den Schalter)
# ------------------------------------------------------------------ #
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def build_write(register, value, count=1):
    payload = value.to_bytes(2*count, "big")
    body = (bytes.fromhex(INTERNAL_ADDR) + bytes([0x10]) + register.to_bytes(2,"big")
            + count.to_bytes(2,"big") + bytes([len(payload)]) + payload)
    return (body + crc16_modbus(body).to_bytes(2,"little")).hex()

def expected_ack(register, count=1):
    body = (bytes.fromhex(INTERNAL_ADDR) + bytes([0x10]) + register.to_bytes(2,"big")
            + count.to_bytes(2,"big"))
    return (body + crc16_modbus(body).to_bytes(2,"little")).hex()

# 0x2032-Nutzdaten (18 Byte), an echten App-Telegrammen verifiziert:
#   [0-1]  Modus         0x0001 Zeitplan / 0x0000 Automatik
#   [2-3]  Entlade-/Einspeise-Sollwert (W, x1)
#   [4-14] fester Block  (027f 028a  0640=Entladegrenze  0001  7f00  8a)
#   [15-16] Ladeleistung (W, x1)
#   [17]   Laden aktiv   0x01 / 0x00
def build_2032_raw(zeitplan, discharge, charge, charge_on):
    payload = ((b"\x00\x01" if zeitplan else b"\x00\x00")
               + int(discharge).to_bytes(2, "big")
               + bytes.fromhex("027f028a064000017f008a")
               + int(charge).to_bytes(2, "big")
               + (b"\x01" if charge_on else b"\x00"))
    body = (bytes.fromhex(INTERNAL_ADDR) + bytes([0x10]) + (0x2032).to_bytes(2, "big")
            + (0x000a).to_bytes(2, "big") + bytes([len(payload)]) + payload)
    return (body + crc16_modbus(body).to_bytes(2, "little")).hex()

def build_2032(watt, zeitplan=True):          # Kompatibilitaet: nur Entlade-Sollwert
    return build_2032_raw(zeitplan, watt, 801, False)

def build_setpoint(watt):
    return build_2032(watt, zeitplan=True)

SETPOINT_ACK = expected_ack(0x2032, 10)
# Kohaerenter Steuerzustand – jeder Schreibbefehl sendet den GANZEN Block.
_state = {"zeitplan": False, "discharge": 0, "charge": 801, "charge_on": False}

# ------------------------------------------------------------------ #
#  Dekodierung (Offsets an echten Geraetedaten verifiziert)
# ------------------------------------------------------------------ #
def parse_f77f(h):
    b = bytes.fromhex(h)
    def W(i, s=False): return int.from_bytes(b[i:i+2], "big", signed=s)
    if len(b) < 70:
        return {}
    v12, i12 = W(24)/100, W(26)/100
    v34, i34 = W(28)/100, W(30)/100
    p12, p34 = round(v12*i12, 1), round(v34*i34, 1)
    ubat, ibat = W(58)/100, W(60, True)/100
    soc, cap = W(62), W(66)
    return {
        "pv1_2_voltage": v12, "pv1_2_current": i12, "pv1_2_power": p12,
        "pv3_4_voltage": v34, "pv3_4_current": i34, "pv3_4_power": p34,
        "pv_power": round(p12+p34),
        "grid_voltage": W(40)/10, "grid_frequency": W(42),
        "ac_power": W(48), "power_factor": W(52)/100,
        "inverter_temp": W(54), "battery_voltage": ubat,
        "battery_current": ibat, "battery_power": round(ubat*ibat),
        "battery_temp": W(68), "soc": soc, "soh": W(64),
        "battery_capacity": cap, "battery_remaining": round(soc/100*cap),
    }

def parse_block09(h):
    b = bytes.fromhex(h)
    def W(i): return int.from_bytes(b[i:i+2], "big")
    if len(b) < 46:
        return {}
    return {
        "energy_today": W(28)/100, "energy_total": W(34)/100,
        "consumption_total": W(38)/100,
        "revenue_today": W(40)/100, "revenue_total": W(44)/100,
    }

# ------------------------------------------------------------------ #
#  WebSocket
# ------------------------------------------------------------------ #
async def ws_pull():
    """Init -> ACK/Begruessung -> leerer Abruf -> Bloecke einsammeln -> Werte."""
    ssl_ctx = ssl.create_default_context()
    frames = []
    ws = await websockets.connect(WS_URL, ssl=ssl_ctx, open_timeout=CONNECT_TIMEOUT)
    async with ws:
        await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": INIT_QUERY}))
        polled = False
        try:
            deadline = asyncio.get_event_loop().time() + GREETING_TIMEOUT
            while asyncio.get_event_loop().time() < deadline and not polled:
                f = await asyncio.wait_for(ws.recv(), timeout=GREETING_TIMEOUT)
                frames.append(f)
                low = f.lower()
                if INIT_ACK in low or "连接成功" in f or '"msg"' in low:
                    await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": ""}))
                    polled = True
        except asyncio.TimeoutError:
            await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": ""}))
        try:
            async with asyncio.timeout(COLLECT_SECONDS):
                while True:
                    frames.append(await ws.recv())
        except (asyncio.TimeoutError, ConnectionClosed):
            pass

    f77f = blk09 = None
    for fr in frames:
        try: d = json.loads(fr)
        except (ValueError, TypeError): continue
        if not (isinstance(d, dict) and d.get("mac","").upper()==DEVICE_MAC.upper()): continue
        m = (d.get("msg") or "").lower()
        if m.startswith("f77f"): f77f = m
        elif m.startswith("09"): blk09 = m
    values = {}
    if f77f: values.update(parse_f77f(f77f))
    if blk09: values.update(parse_block09(blk09))
    return values

async def ws_send_telegram(telegram, want_ack):
    """Beliebiges Schreib-Telegramm senden und gegen die erwartete ACK prüfen."""
    ssl_ctx = ssl.create_default_context()
    want = want_ack.lower()
    ws = await websockets.connect(WS_URL, ssl=ssl_ctx, open_timeout=CONNECT_TIMEOUT)
    async with ws:
        await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": telegram}))
        try:
            async with asyncio.timeout(GREETING_TIMEOUT):
                while True:
                    frame = await ws.recv()
                    try:
                        d = json.loads(frame)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(d, dict) and d.get("msg", "").lower() == want:
                        return True
        except (asyncio.TimeoutError, ConnectionClosed):
            return False
    return False

async def ws_write(register, value):
    ssl_ctx = ssl.create_default_context()
    telegram = build_write(register, value); want = expected_ack(register).lower()
    ws = await websockets.connect(WS_URL, ssl=ssl_ctx, open_timeout=CONNECT_TIMEOUT)
    async with ws:
        await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": telegram}))
        try:
            async with asyncio.timeout(GREETING_TIMEOUT):
                while True:
                    frame = await ws.recv()
                    try:
                        d = json.loads(frame)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(d, dict) and d.get("msg", "").lower() == want:
                        return True
        except (asyncio.TimeoutError, ConnectionClosed):
            return False
    return False

# ------------------------------------------------------------------ #
#  Discovery
# ------------------------------------------------------------------ #
async def publish_discovery(client):
    async def cfg(component, object_id, payload):
        topic = f"{DISCOVERY_PREFIX}/{component}/{NODE_ID}/{object_id}/config"
        payload["device"] = DEVICE_INFO
        payload["availability_topic"] = T_AVAIL
        await client.publish(topic, json.dumps(payload), retain=True)

    for sid, name, unit, dc, sc, icon in SENSORS:
        p = {"name": name, "unique_id": f"ecovis_{sid}", "object_id": f"ecovis_{sid}",
             "state_topic": T_SENSOR(sid)}
        if unit: p["unit_of_measurement"] = unit
        if dc:   p["device_class"] = dc
        if sc:   p["state_class"] = sc
        if icon: p["icon"] = icon
        await cfg("sensor", sid, p)

    await cfg("switch", "reverse_feed", {
        "name": "Rückstromeinspeisung", "unique_id": "ecovis_reverse_feed",
        "object_id": "ecovis_reverse_feed", "command_topic": T_FEED_SET,
        "state_topic": T_FEED_STATE, "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:transmission-tower-import"})
    await cfg("button", "telemetry_refresh", {
        "name": "Telemetrie aktualisieren", "unique_id": "ecovis_telemetry_refresh",
        "object_id": "ecovis_telemetry_refresh", "command_topic": T_TELE_REFR,
        "icon": "mdi:refresh"})
    await cfg("number", "einspeiseleistung", {
        "name": "Einspeiseleistung", "unique_id": "ecovis_einspeiseleistung",
        "object_id": "ecovis_einspeiseleistung", "command_topic": T_SET_CMD,
        "state_topic": T_SET_STATE, "min": 0, "max": MAX_WATTS, "step": 10,
        "unit_of_measurement": "W", "mode": "box",
        "icon": "mdi:transmission-tower-export"})
    await cfg("select", "betriebsmodus", {
        "name": "Betriebsmodus", "unique_id": "ecovis_betriebsmodus",
        "object_id": "ecovis_betriebsmodus", "command_topic": T_MODE_CMD,
        "state_topic": T_MODE_STATE, "options": ["Automatik", "Zeitplan"],
        "icon": "mdi:tune-variant"})
    await cfg("number", "ladeleistung", {
        "name": "Ladeleistung", "unique_id": "ecovis_ladeleistung",
        "object_id": "ecovis_ladeleistung", "command_topic": T_CHG_CMD,
        "state_topic": T_CHG_STATE, "min": 0, "max": MAX_WATTS, "step": 10,
        "unit_of_measurement": "W", "mode": "box",
        "icon": "mdi:transmission-tower-import"})
    await cfg("sensor", "connection", {
        "name": "ECOVIS Verbindung", "unique_id": "ecovis_connection",
        "object_id": "ecovis_connection", "state_topic": T_CONN_STATE,
        "icon": "mdi:lan-connect"})
    log.info("Discovery veroeffentlicht (%d Sensoren + Schalter/Button).", len(SENSORS))

# ------------------------------------------------------------------ #
#  Aktionen
# ------------------------------------------------------------------ #
async def do_telemetry(client):
    try:
        values = await asyncio.wait_for(ws_pull(), timeout=CONNECT_TIMEOUT+COLLECT_SECONDS+5)
    except Exception as e:
        log.warning("Telemetrie fehlgeschlagen: %s", e)
        await client.publish(T_CONN_STATE, "degraded", retain=True)
        return
    if not values:
        log.warning("Keine Telemetrie-Bloecke erhalten (App verbunden?).")
        await client.publish(T_CONN_STATE, "degraded", retain=True)
        return
    for sid, *_ in SENSORS:
        if sid in values:
            await client.publish(T_SENSOR(sid), str(values[sid]), retain=True)
    await client.publish(T_CONN_STATE, "online", retain=True)
    log.info("Telemetrie ok: PV=%sW AC=%sW SOC=%s%%",
             values.get("pv_power"), values.get("ac_power"), values.get("soc"))

async def do_reverse_feed(client, on):
    try:
        ok = await asyncio.wait_for(ws_write(0x2031, 1 if on else 0), timeout=CONNECT_TIMEOUT+6)
    except Exception as e:
        ok = False; log.warning("Schalten Fehler: %s", e)
    if ok:
        await client.publish(T_FEED_STATE, "ON" if on else "OFF", retain=True)
        log.info("Rueckstromeinspeisung %s", "EIN" if on else "AUS")
    else:
        log.warning("Schalten nicht bestaetigt.")

async def _write_block(client, label):
    telegram = build_2032_raw(_state["zeitplan"], _state["discharge"],
                              _state["charge"], _state["charge_on"])
    log.info("SCHREIBE 0x2032 (%s) | TX=%s", label, telegram)
    try:
        ok = await asyncio.wait_for(ws_send_telegram(telegram, SETPOINT_ACK),
                                    timeout=CONNECT_TIMEOUT+6)
    except Exception as e:
        ok = False; log.warning("0x2032-Fehler: %s", e)
    if ok:
        await client.publish(T_MODE_STATE, "Zeitplan" if _state["zeitplan"] else "Automatik", retain=True)
        await client.publish(T_SET_STATE, str(_state["discharge"]), retain=True)
        await client.publish(T_CHG_STATE, str(_state["charge"] if _state["charge_on"] else 0), retain=True)
        log.info("0x2032 bestaetigt (%s)", label)
    else:
        log.warning("0x2032 nicht bestaetigt (App verbunden? nichts gesetzt).")

async def do_set_power(client, watt):
    _state["discharge"] = max(0, min(int(round(watt)), MAX_WATTS))
    if _state["discharge"] > 0:
        _state["charge_on"] = False          # Einspeisen -> Laden aus (nur eins aktiv)
    _state["zeitplan"] = True
    await _write_block(client, f"Einspeisen {_state['discharge']}W")

async def do_set_charge(client, watt):
    w = max(0, min(int(round(watt)), MAX_WATTS))
    if w > 0:
        _state["charge"] = w
        _state["charge_on"] = True
        _state["discharge"] = 0              # Laden -> Entlade-Sollwert auf 0 (nur eins aktiv)
    else:
        _state["charge_on"] = False
    _state["zeitplan"] = True
    await _write_block(client, f"Laden {w}W" if w > 0 else "Laden aus")

async def do_set_mode(client, zeitplan):
    _state["zeitplan"] = zeitplan
    await _write_block(client, "Zeitplan" if zeitplan else "Automatik")

# ------------------------------------------------------------------ #
#  Loops
# ------------------------------------------------------------------ #
async def telemetry_loop(client):
    while True:
        await do_telemetry(client)
        await asyncio.sleep(TELEMETRY_INTERVAL)

async def message_loop(client):
    await client.subscribe(T_FEED_SET)
    await client.subscribe(T_TELE_REFR)
    await client.subscribe(T_SET_CMD)
    await client.subscribe(T_MODE_CMD)
    await client.subscribe(T_CHG_CMD)
    async for m in client.messages:
        t = str(m.topic); p = m.payload.decode(errors="replace").strip()
        try:
            if t == T_FEED_SET:  await do_reverse_feed(client, p.upper() == "ON")
            elif t == T_TELE_REFR: await do_telemetry(client)
            elif t == T_SET_CMD:
                try: watt = float(p)
                except ValueError: log.warning("Ungueltiger Sollwert: %r", p); continue
                await do_set_power(client, watt)
            elif t == T_CHG_CMD:
                try: watt = float(p)
                except ValueError: log.warning("Ungueltige Ladeleistung: %r", p); continue
                await do_set_charge(client, watt)
            elif t == T_MODE_CMD:
                await do_set_mode(client, p.strip().lower().startswith("zeit"))
        except Exception as e:
            log.exception("Kommando-Fehler: %s", e)

async def run():
    will = aiomqtt.Will(topic=T_AVAIL, payload="offline", retain=True)
    log.info("Verbinde MQTT %s:%d ...", MQTT_HOST, MQTT_PORT)
    async with aiomqtt.Client(hostname=MQTT_HOST, port=MQTT_PORT,
                              username=MQTT_USER, password=MQTT_PASS, will=will) as client:
        await client.publish(T_AVAIL, "online", retain=True)
        await publish_discovery(client)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(telemetry_loop(client))
            tg.create_task(message_loop(client))

def main():
    import time
    while True:
        try:
            asyncio.run(run())
        except* Exception as eg:
            for e in eg.exceptions:
                log.error("Bridge-Fehler: %s", e)
            log.info("Neustart in 5 s ...")
            time.sleep(5)

if __name__ == "__main__":
    main()
