# ESS-AIO v9.9.27 patch notes

- PCS Control web table no longer shows BMS-style SOC/voltage/current/status under Last/Latest Value.
- PCS Control web table now focuses on AC breaker/contactor, DC breaker/contactor, and run/power-on status.
- Connect/Disconnect buttons are renamed to Connect Polling / Disconnect Polling to distinguish them from PCS Start/Stop commands.
- BMS Live Devices action column now includes per-device Clear Fault and one-shot Heartbeat buttons.
- BMS Control Register Panel is moved behind an Advanced manual register write details block.
- Added BMS Version Read panel: reads MBMU/ETH and a configurable SBMU count. SBMU01 uses configured base; SBMU02+ follow +0x400 spacing in the client.
- CSV status now returns BMS/PCS output directory fields so the Web UI/API can show where files are being written.
- Curves now support BMS Status and BMS Online Rack Count signals in the selector.
- Overview now includes compact monitoring trend cards for BMS status and rack count.

Notes:
- In the included Kehua PCS profile, Close DC/Open DC map to holding register 7800 with write values 1/0. Status is read from AC 7030 and DC 7031.
- In the NR template, Close DC/Open DC map to coil 5 with FC05 true/false, but its status point is still marked as field-validation placeholder.
