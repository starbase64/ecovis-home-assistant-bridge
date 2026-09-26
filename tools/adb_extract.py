#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adb_extract.py - zieht Telemetrie-Bloecke aus einem ECO-WORTHY ADB-logcat.

Die App loggt jeden empfangenen Frame als  raw={"msg":"<hex>","mac":"..."} .
Kurze Bloecke (f77f-Kopf, 02, 09) stehen dort vollstaendig; die langen Bloecke
03-08 kuerzt die App im Log selbst (".. .(len=NNN)") - die werden uebersprungen.
Fuer die Werte-Zuordnung ist vor allem der f77f-Kopf wichtig.

Ablauf:
  1. Handy per ADB, App komplett schliessen, dann:
        .\adb logcat -c
        .\adb logcat > cap1.txt
     App oeffnen, Startseite (PV/Netz/Hauslast/SOC ablesen), ~10s warten, Strg+C.
  2. python adb_extract.py cap1.txt --pv 939 --grid 333 --load 1 --soc 78 --out captures.json
  3. Schritte 1-2 zwei- bis dreimal zu UNTERSCHIEDLICHEN Betriebspunkten
     (viel PV / wenig PV / einmal Netzeinspeisung = grid negativ).
  4. python ecovis_decode.py captures.json

Reines Python, keine Abhaengigkeiten. Laeuft unter Windows/PowerShell und Linux.
"""

import argparse
import json
import os
import re
import sys

MAC = "AABBCCDDEEFF"   # <-- eigene Geraete-MAC

# vollstaendig geloggte msg-Hex:  "msg":"<hex>"  (endet sauber mit Anfuehrungszeichen)
MSG_RE = re.compile(r'"msg"\s*:\s*"([0-9a-fA-F]{4,})"')

# gueltige Block-Anfaenge (Kopf f77f oder Bloecke 02..09)
BLOCK_HEADS = ("f77f", "02", "03", "04", "05", "06", "07", "08", "09")


def is_block(hexstr):
    h = hexstr.lower()
    if len(h) % 2:
        return False
    if h.startswith("f77f"):
        return True
    return h[:2] in {"02", "03", "04", "05", "06", "07", "08", "09"}


def extract_frames(text):
    """Alle vollstaendigen Telemetrie-Bloecke in Reihenfolge (mit Duplikaten)."""
    out = []
    for m in MSG_RE.finditer(text):
        h = m.group(1).lower()
        if is_block(h):
            out.append(h)
    return out


def first_burst(frames):
    """Ersten zusammenhaengenden Burst zurueckgeben: ab f77f bis vor das naechste f77f."""
    # Startindex des ersten f77f
    start = next((i for i, h in enumerate(frames) if h.startswith("f77f")), None)
    if start is None:
        return frames[:]                       # kein Kopf gefunden -> alles nehmen
    burst = [frames[start]]
    for h in frames[start + 1:]:
        if h.startswith("f77f"):               # naechster Burst beginnt
            break
        burst.append(h)
    # innerhalb des Bursts Duplikate entfernen, Reihenfolge halten
    seen, uniq = set(), []
    for h in burst:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


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


def main():
    p = argparse.ArgumentParser(description="Telemetrie-Bloecke aus ADB-logcat ziehen")
    p.add_argument("logfile", help="gespeicherte logcat-Datei (z. B. cap1.txt)")
    p.add_argument("--pv", type=float)
    p.add_argument("--grid", type=float)
    p.add_argument("--load", type=float)
    p.add_argument("--soc", type=float)
    p.add_argument("--out", help="captures.json zum Anhaengen")
    a = p.parse_args()

    try:
        data = open(a.logfile, "rb").read()
    except OSError as e:
        sys.exit(f"Kann Datei nicht lesen: {e}")
    # PowerShell "> datei.txt" schreibt UTF-16; ADB/Linux meist UTF-8.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    elif b"\x00" in data[:200]:                # Nullbytes -> vermutlich UTF-16 ohne BOM
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")

    frames = extract_frames(text)
    if not frames:
        sys.exit("Keine vollstaendigen Telemetrie-Bloecke gefunden. "
                 "Kam die Telemetrie im Log an? (f77f/02/09-Zeilen)")

    burst = first_burst(frames)
    heads = [b[:4] if b.startswith('f77f') else b[:2] for b in burst]
    print(f"{len(frames)} Bloecke gesamt, erster Burst: {len(burst)} Bloecke "
          f"[{', '.join(heads)}]")
    for b in burst:
        print(f"  {b[:2] if not b.startswith('f77f') else 'f77f'}: {b}")
    combined = "".join(burst)
    print("\nKombiniert (Feld \"hex\"):")
    print(combined)

    if a.out:
        values = {k: v for k, v in
                  (("pv", a.pv), ("grid", a.grid), ("load", a.load), ("soc", a.soc))
                  if v is not None}
        if not values:
            print("\nHinweis: --out ohne Werte (--pv/--grid/--load/--soc); nichts "
                  "angehaengt.", file=sys.stderr)
        else:
            n = append_capture(a.out, combined, values)
            print(f"\nAn {a.out} angehaengt (jetzt {n} Mitschnitt(e)). Werte: {values}")


if __name__ == "__main__":
    main()
