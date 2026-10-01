#!/usr/bin/env python3
"""Look up the OCR'd ply number in master.csv and light the matching bin on the ESP32.

master.csv needs two columns: the ply number (--key-col, auto-detected from common names) and the bin
(--bin-col, default 'no_of_ply'). The bin value may be a relay number 1-4, or a bin label such as 'Before-DW'
that bin_light_controller.DEFAULT_BIN_TO_RELAY maps to a relay.

Three ways to run it:
  follow the pipeline (no change to rtracker_ocr.py, it already flushes every confirmed roll to that CSV):
      python3 ply_bin_lights.py --csv master.csv --port /dev/ttyUSB0 --watch output/readings.csv
  one-shot test of a single ply number:
      python3 ply_bin_lights.py --csv master.csv --port /dev/ttyUSB0 --ply 80
  type ply numbers by hand (no camera, no serial port = dry run that only prints):
      python3 ply_bin_lights.py --csv master.csv

Or import it: PlyBinLights(...).show(ply) after a reading is confirmed.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from bin_light_controller import BinLightController

KEY_CANDIDATES = ("ply", "ply_no", "plyno", "ply no", "ply_number", "ply number", "ply_num")
DEFAULT_BIN_COL = "no_of_ply"


def norm_ply(value) -> str:
    """Match the OCR text against the CSV loosely: ' 080 ', '80.0' and '80' are all the same ply."""
    s = str(value).strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    return s.lstrip("0") or "0"


def load_master(path: Path, key_col: str | None = None, bin_col: str = DEFAULT_BIN_COL):
    """-> ({normalized ply: relay int or bin label}, key_col, bin_col). Other columns are ignored."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path} has no rows")
    cols = [c for c in rows[0] if c]
    lower = {c.lower().strip(): c for c in cols}

    if key_col is None:
        key_col = next((lower[c] for c in KEY_CANDIDATES if c in lower), None)
        if key_col is None:
            raise SystemExit(f"no ply column found in {path} (columns: {cols}) -- pass --key-col")
    elif key_col not in cols:
        key_col = lower.get(key_col.lower().strip(), key_col)
    if bin_col not in cols:
        bin_col = lower.get(bin_col.lower().strip(), bin_col)
    for c in (key_col, bin_col):
        if c not in cols:
            raise SystemExit(f"column {c!r} not in {path} (columns: {cols})")

    table = {}
    for r in rows:
        key, val = norm_ply(r.get(key_col, "")), str(r.get(bin_col, "")).strip()
        if not key or not val:
            continue
        try:
            table[key] = int(float(val))  # plain relay number
        except ValueError:
            table[key] = val              # bin label, resolved via BinLightController.bin_map
    print(f"{path}: {len(table)} plys  (ply column '{key_col}', bin column '{bin_col}')")
    return table, key_col, bin_col


class PlyBinLights:
    def __init__(self, master_csv, port=None, baud=115200, key_col=None, bin_col=DEFAULT_BIN_COL, hold=0.0):
        self.table, self.key_col, self.bin_col = load_master(Path(master_csv), key_col, bin_col)
        self.lights = BinLightController(port=port, baud=baud)
        self.hold = float(hold)  # 0 = keep the bin lit until the next roll is read
        self._lit_at = None

    def open(self):
        self.lights.open()

    def close(self):
        self.lights.close()

    def resolve(self, ply):
        """-> relay number, bin label, or None if the ply is not in master.csv."""
        return self.table.get(norm_ply(ply))

    def show(self, ply) -> bool:
        target = self.resolve(ply)
        if target is None:
            print(f"ply {ply!r}: not in master.csv -- lights unchanged")
            return False
        print(f"ply {ply} -> bin {target}")
        if isinstance(target, int):
            self.lights.set_relay(target)
        else:
            self.lights.set_bin(target)
        self._lit_at = time.time()
        return True

    def tick(self):
        """Call regularly when --hold is set: switches the bin off again after that many seconds."""
        if self.hold and self._lit_at and time.time() - self._lit_at >= self.hold:
            self.lights.all_off()
            self._lit_at = None


def watch(readings_csv, lights: PlyBinLights, poll=0.4):
    """Follow output/readings.csv (timestamp, track_id, ply, start, end) and light each new confirmed roll."""
    path = Path(readings_csv)
    print(f"watching {path} -- Ctrl-C to stop")
    while not path.exists():  # the pipeline may not have written it yet
        time.sleep(poll)
    with open(path, newline="", encoding="utf-8") as f:
        f.seek(0, 2)  # only rows appended from now on
        while True:
            line = f.readline()
            if not line:
                lights.tick()
                time.sleep(poll)
                continue
            row = next(csv.reader([line.strip()]), [])
            if len(row) >= 3 and row[0] != "timestamp":
                lights.show(row[2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="master.csv", help="master table with the ply -> bin mapping")
    ap.add_argument("--port", default=None, help="ESP32 serial port, e.g. /dev/ttyUSB0 (omit = dry run, prints only)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--key-col", default=None, help="ply column name (default: auto-detect)")
    ap.add_argument("--bin-col", default=DEFAULT_BIN_COL, help=f"bin column name (default: {DEFAULT_BIN_COL})")
    ap.add_argument("--hold", type=float, default=0.0, help="seconds to keep a bin lit (0 = until the next roll)")
    ap.add_argument("--watch", default=None, help="follow this readings CSV, e.g. output/readings.csv")
    ap.add_argument("--ply", default=None, help="one-shot: light the bin for this ply number and exit")
    args = ap.parse_args()

    lights = PlyBinLights(args.csv, args.port, args.baud, args.key_col, args.bin_col, args.hold)
    lights.open()
    try:
        if args.ply:
            lights.show(args.ply)
            time.sleep(args.hold or 2.0)
        elif args.watch:
            watch(args.watch, lights)
        else:
            print("type a ply number per line (Ctrl-D to quit)")
            for line in sys.stdin:
                if line.strip():
                    lights.show(line.strip())
    except KeyboardInterrupt:
        pass
    finally:
        lights.close()


if __name__ == "__main__":
    main()
