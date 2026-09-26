#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecovis_capture.py - nimmt EINEN vollstaendigen Telemetrie-Mitschnitt auf.

Echter Ablauf (aus ADB-Log rekonstruiert, KEIN Login noetig):
  1. Init senden:  {"mac":MAC,"msg":"FF10070187240014ffffF6D6"}
  2. Geraet bestaetigt mit ff10004c30 (Begruessung 连接成功 kommt irgendwann)
  3. Leeren Abruf senden: {"mac":MAC,"msg":""}
  4. Geraet streamt die Bloecke f77f + 02..09  <- das ist die Telemetrie
  5. Fuer frische Werte einfach wieder "" senden

WICHTIG: Die Cloud erlaubt offenbar nur EINE aktive Sitzung pro Geraet.
Die ECO-WORTHY-App daher VORHER komplett schliessen (aus dem App-Wechsler
wischen), sonst schliesst der Server unsere Verbindung sofort (Code 1000).

Aufrufe:
  python ecovis_capture.py
  python ecovis_capture.py --pv 939 --grid 333 --load 1 --soc 78 --out captures.json

Abhaengigkeit:  pip install "websockets>=10.4"
"""

import argparse
import asyncio
import json
import os
import ssl
import sys

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    sys.exit("Fehlt: pip install \"websockets>=10.4\"")

WS_URL     = "wss://app.eco-worthy.com/ws/websocket"
DEVICE_MAC = "AABBCCDDEEFF"   # <-- eigene Geraete-MAC (App: Geraeteinformationen)
INIT_QUERY = "FF10070187240014ffffF6D6"
INIT_ACK   = "ff10004c30"
GREETING_TIMEOUT = 4.0
COLLECT_SECONDS  = 6.0


def extract_blocks(frames, mac):
    """Aus rohen JSON-Frames die Telemetrie-msg des Zielgeraets ziehen."""
    blocks = []
    for frame in frames:
        try:
            data = json.loads(frame)
        except (ValueError, TypeError):
            continue
        if not (isinstance(data, dict) and data.get("mac", "").upper() == mac.upper()):
            continue
        msg = (data.get("msg") or "").lower()
        # nur echte Telemetrie behalten: f77f-Kopf oder Bloecke 02..09;
        # Init-ACK (ff10..) und leere Frames ignorieren
        if msg.startswith("f77f") or (len(msg) >= 2 and msg[:2] in
                                      {"02", "03", "04", "05", "06", "07", "08", "09"}):
            blocks.append(msg)
    return blocks


def append_capture(path, hexstr, values):
    caps = []
    if os.path.exists(path):
        try:
            caps = json.load(open(path, encoding="utf-8"))
            if not isinstance(caps, list):
                caps = []
        except (ValueError, OSError):
            caps = []
    caps.append({"hex": hexstr, "values": values})
    json.dump(caps, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return len(caps)


async def capture():
    ssl_ctx = ssl.create_default_context()
    frames = []
    print(f"Verbinde {WS_URL} ...")
    ws = await websockets.connect(WS_URL, ssl=ssl_ctx, open_timeout=10)
    async with ws:
        # 1) Init senden
        print(f"  Sende Init: {INIT_QUERY}")
        await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": INIT_QUERY}))

        # 2)+3) auf Begruessung/ACK warten, dann leeren Abruf senden
        polled = False
        try:
            deadline = asyncio.get_event_loop().time() + GREETING_TIMEOUT
            while asyncio.get_event_loop().time() < deadline and not polled:
                f = await asyncio.wait_for(ws.recv(), timeout=GREETING_TIMEOUT)
                print(f"  <- {f[:80]!r}")
                frames.append(f)
                low = f.lower()
                if (INIT_ACK in low) or ("连接成功" in f) or ('"msg"' in low):
                    print("  Sende leeren Abruf: \"\"")
                    await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": ""}))
                    polled = True
        except asyncio.TimeoutError:
            print("  (keine Begruessung/ACK - sende Abruf trotzdem)")
            await ws.send(json.dumps({"mac": DEVICE_MAC, "msg": ""}))
        except ConnectionClosed as e:
            print(f"  Server hat sofort geschlossen: {e}", file=sys.stderr)
            print("  -> App noch verbunden? (nur eine Sitzung pro Geraet) oder "
                  "Rate-Limit. App schliessen, 5-10 Min warten.", file=sys.stderr)
            return None

        # 4) Telemetrie-Burst einsammeln
        try:
            async with asyncio.timeout(COLLECT_SECONDS):
                while True:
                    f = await ws.recv()
                    print(f"  Frame: {f[:90]!r}{' ...' if len(f) > 90 else ''}")
                    frames.append(f)
        except asyncio.TimeoutError:
            pass
        except ConnectionClosed as e:
            print(f"  Verbindung geschlossen: {e}")

    blocks = extract_blocks(frames, DEVICE_MAC)
    if not blocks:
        print("\nKeine Telemetrie-Bloecke empfangen.", file=sys.stderr)
        print("Meist: App noch verbunden (eine Sitzung pro Geraet) oder Rate-Limit.",
              file=sys.stderr)
        return None

    print("\nEinzelne Bloecke:")
    for i, b in enumerate(blocks):
        print(f"  [{i}] {b}")
    combined = "".join(blocks)
    print("\nKombiniert (Feld \"hex\"):")
    print(combined)
    return combined


def main():
    p = argparse.ArgumentParser(description="ECOVIS Telemetrie-Mitschnitt")
    p.add_argument("--pv", type=float)
    p.add_argument("--grid", type=float)
    p.add_argument("--load", type=float)
    p.add_argument("--soc", type=float)
    p.add_argument("--out")
    a = p.parse_args()

    combined = asyncio.run(capture())
    if not combined:
        sys.exit(1)

    if a.out:
        values = {k: v for k, v in
                  (("pv", a.pv), ("grid", a.grid), ("load", a.load), ("soc", a.soc))
                  if v is not None}
        if not values:
            print("\nHinweis: --out ohne Werte angegeben; nichts angehaengt.",
                  file=sys.stderr)
        else:
            n = append_capture(a.out, combined, values)
            print(f"\nAn {a.out} angehaengt (jetzt {n} Mitschnitt(e)). Werte: {values}")


if __name__ == "__main__":
    main()
