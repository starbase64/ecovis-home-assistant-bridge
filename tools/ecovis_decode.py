#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecovis_decode.py – Feld-Finder für die ECOVIS-Rohtelemetrie.

Prinzip: Man nimmt mehrere Telemetrie-Mitschnitte zu UNTERSCHIEDLICHEN
Betriebspunkten auf und notiert zu jedem die gleichzeitig in der App
angezeigten Werte (PV, Netz, Hauslast, SOC …). Das Tool sucht dann für jede
Größe die Byte-Position, deren dekodierter Wert in ALLEN Mitschnitten passt.
Nur so verschwinden die vielen Zufallstreffer eines einzelnen Mitschnitts.

Eingabe: eine JSON-Datei mit einer Liste von Mitschnitten:

[
  {
    "hex": "f77f09....02....09....",          # Roh-Hex der Telemetrie-Antwort
    "values": {"pv": 939, "grid": 333, "load": 1, "soc": 78}
  },
  {
    "hex": "f77f09....",
    "values": {"pv": 210, "grid": -180, "load": 40, "soc": 74}
  }
]

Aufruf:
  python ecovis_decode.py captures.json
  python ecovis_decode.py captures.json --tol 3         # Toleranz in Einheiten
  python ecovis_decode.py --selftest                    # ohne Datei, prüft die Logik
"""

import argparse
import json
import sys

# Kandidaten-Skalierungen (Rohwert * Faktor = Anzeigewert)
SCALES = [1.0, 0.1, 0.01, 0.001, 10.0]
WIDTHS = [2, 4]                         # 16-bit und 32-bit
ENDIANS = ["big", "little"]
SIGNS = [False, True]                   # unsigned / signed


def decode_at(raw: bytes, offset: int, width: int, endian: str, signed: bool):
    if offset + width > len(raw):
        return None
    return int.from_bytes(raw[offset:offset + width], endian, signed=signed)


def candidates_for(captures, quantity, tol):
    """Alle (offset,width,endian,sign,scale), die 'quantity' in ALLEN Mitschnitten erklären."""
    raws = [bytes.fromhex(c["hex"]) for c in captures]
    targets = [c["values"][quantity] for c in captures]
    n = min(len(r) for r in raws)
    hits = []
    for width in WIDTHS:
        for offset in range(0, n - width + 1):
            for endian in ENDIANS:
                for signed in SIGNS:
                    raw_vals = [decode_at(r, offset, width, endian, signed) for r in raws]
                    if any(v is None for v in raw_vals):
                        continue
                    for scale in SCALES:
                        ok = all(abs(rv * scale - tgt) <= tol
                                 for rv, tgt in zip(raw_vals, targets))
                        if ok:
                            hits.append({
                                "offset": offset, "width": width,
                                "endian": endian, "signed": signed,
                                "scale": scale,
                                "decoded": [round(rv * scale, 3) for rv in raw_vals],
                            })
    # Kürzere Felder (2 Byte) und Faktor 1 zuerst, dann nach Offset
    hits.sort(key=lambda h: (h["width"], abs(h["scale"] - 1.0), h["offset"]))
    return hits


def run(captures, tol):
    quantities = sorted({q for c in captures for q in c["values"]})
    print(f"{len(captures)} Mitschnitt(e), Toleranz ±{tol}\n")
    for q in quantities:
        targets = [c["values"][q] for c in captures]
        print(f"=== {q}  (Zielwerte {targets}) ===")
        hits = candidates_for(captures, q, tol)
        if not hits:
            print("  keine passende Byte-Position gefunden "
                  "(mehr/andere Mitschnitte oder größere Toleranz nötig)\n")
            continue
        for h in hits[:8]:
            sign = "s" if h["signed"] else "u"
            print(f"  Offset {h['offset']:>3}  {h['width']*8}bit-{sign} "
                  f"{h['endian']:<6} x{h['scale']:<5} -> {h['decoded']}")
        if len(hits) > 8:
            print(f"  … und {len(hits) - 8} weitere")
        if len(hits) == 1:
            print("  ^ eindeutig.")
        elif len(captures) < 3:
            print("  Mehrdeutig – ein weiterer Mitschnitt an einem anderen "
                  "Betriebspunkt grenzt es weiter ein.")
        print()


def selftest():
    # Synthetische Telemetrie: pv @ off 4 (x1, big), soc @ off 10 (x1),
    # grid @ off 6 (x1, signed), voltage @ off 12 (x0.01)
    import struct

    def make(pv, grid, soc, volt_cV):
        buf = bytearray(40)
        struct.pack_into(">H", buf, 4, pv)
        struct.pack_into(">h", buf, 6, grid)          # signed
        struct.pack_into(">H", buf, 10, soc)
        struct.pack_into(">H", buf, 12, volt_cV)      # z.B. 5375 -> 53.75V
        # etwas Rauschen, das zufällig mal einen Zielwert trifft:
        buf[20] = 78
        return buf.hex()

    caps = [
        {"hex": make(939, 333, 78, 5375), "values": {"pv": 939, "grid": 333, "soc": 78, "volt": 53.75}},
        {"hex": make(210, -180, 74, 5378), "values": {"pv": 210, "grid": -180, "soc": 74, "volt": 53.78}},
        {"hex": make(1200, 50, 80, 5390), "values": {"pv": 1200, "grid": 50, "soc": 80, "volt": 53.90}},
    ]
    print(">>> Selbsttest mit synthetischen Daten "
          "(erwartet: pv@4, grid@6 signed, soc@10, volt@12 x0.01)\n")
    run(caps, tol=1)


def main():
    p = argparse.ArgumentParser(description="ECOVIS Telemetrie-Feldfinder")
    p.add_argument("captures", nargs="?", help="JSON-Datei mit Mitschnitten")
    p.add_argument("--tol", type=float, default=2.0, help="Toleranz in Anzeige-Einheiten")
    p.add_argument("--selftest", action="store_true", help="Logik mit Testdaten prüfen")
    a = p.parse_args()
    if a.selftest:
        selftest()
        return
    if not a.captures:
        p.error("Bitte JSON-Datei angeben oder --selftest verwenden.")
    with open(a.captures, encoding="utf-8") as f:
        caps = json.load(f)
    if not isinstance(caps, list) or not caps:
        sys.exit("JSON muss eine nicht-leere Liste von Mitschnitten sein.")
    run(caps, a.tol)


if __name__ == "__main__":
    main()
