# v9.9.30 LTS - BMS Rack/SBMU Monitor Patch

- Fixed BMS connection state after temporary communication loss: fresh snapshots now override stale error counters, so recovered devices show online.
- Added BMS Live Devices Online Racks column using V22 0x0304 / 0x0305 style fields.
- Added BMS Control Rack / SBMU Monitor section for selected BMS.
- Rack/SBMU monitor reads configurable rack count and displays online, SOC, voltage, current, power, cell voltage sum, relay and ready status.
- Added staged Rack Enable/Disable UI: row selection does not write immediately; Apply writes the combined 0x038D/0x038E/0x038F bitmask.
- Apply flow reads current mask first, calculates target mask, writes only changed masks, then reads back for verification.
