#!/usr/bin/env python3
"""Create a starter PCS profile JSON.

Usage:
  python tools/pcs_profile_skeleton.py --vendor NR --model PCS-9567AN --out pcs_profiles/nr_9567.json

This script intentionally creates a skeleton only. Always verify addresses,
function codes, scale, sign convention, and address_offset against packet capture
or the vendor protocol document before field use.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    key = f"{args.vendor}_{args.model}".lower().replace(" ", "_").replace("/", "_")
    profile = {
        "profile_key": key,
        "display_name": f"{args.vendor} {args.model}",
        "vendor": args.vendor,
        "model": args.model,
        "driver": "generic_modbus_pcs",
        "host": "192.168.1.100",
        "port": 502,
        "unit_id": 1,
        "timeout": 3.0,
        "address_offset": 0,
        "power_sign": {"positive": "discharge", "negative": "charge"},
        "capabilities": {
            "active_power_control": {"enabled": True, "point": "set_active_power"},
            "reactive_power_control": {"enabled": False, "point": "set_reactive_power"},
        },
        "commands": {},
        "point_aliases": {},
        "points": {
            "active_power": {
                "register_type": "input",
                "address": 0,
                "scale": 1.0,
                "data_type": "INT16",
                "register_count": 1,
                "unit": "kW",
                "access": "RO",
            },
            "set_active_power": {
                "register_type": "holding",
                "address": 0,
                "scale": 1.0,
                "data_type": "INT16",
                "register_count": 1,
                "unit": "kW",
                "access": "WO",
                "write_function": "single",
            },
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
