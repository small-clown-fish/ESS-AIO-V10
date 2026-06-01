
## v9.9.26 LTS Dashboard + BMS Status View

- Dashboard redesigned as card + chart style site overview.
- BMS Live Devices now shows SOC, voltage, current, power, update time and friendly status labels as table columns.
- BMS status mapping: 1=Normal, 2=Full Charge, 3=Full Discharge, 4=Warning, 5=Fault.
- Raw BMS status numbers are no longer shown in the live device status column.

# Release Notes

## v9.9.24 Web Lite Health Check Fix

This release fixes the Web-only launcher false alarm where Runtime was actually running and returned `{ok: true}`, but Web.exe still displayed "Runtime not reachable" because the launcher used an overly strict api_schema text check.

Use `ESS-AIO-Web.exe` from `dist/ESS-AIO-Web-Lite/`. Closing the browser still does not stop Runtime; use the Web shutdown page or `ESS-AIO-Shutdown.exe`.

# RELEASE NOTES

## v9.9.23 Web Lite Packaging Repair

Use `ESS-AIO-Web.exe` from the top level of the Web-Lite folder. Do not move EXE files out of the folder. Runtime is bundled under `ESS-AIO-Runtime/` and Shutdown is bundled as `ESS-AIO-Shutdown.exe`.

If the browser is unreachable, check `logs/runtime_stdout.log`, `logs/runtime_stderr.log`, and `%LOCALAPPDATA%/ESS-AIO/logs/runtime_fatal.log`.


## v9.9.25 LTS Field UX Dashboard Fix

- Added visible BMS heartbeat feedback so operators can see when heartbeat start/stop commands are sent and acknowledged.
- Restored explicit Clear Fault All Online button in BMS Control.
- Fixed Register Debug device list to include live BMS device_states and configured Project BMS/PCS.
- Enhanced BMS Live Devices with SOC / voltage / current / status summary columns.
- Upgraded Overview into a site dashboard with device online counts, CSV state, strategy count and active issue summary.
- Added runtime footer to each active page instead of only showing runtime status in the header.
- Swapped BMS Live Devices above BMS Control Register Panel for a safer operator workflow.
- Added PCS alarm/fault summary section.
- Made Analyzer more visually guided with upload/select/analyze steps.
- Fixed Curves live fallback and the const CURVES assignment bug that prevented curve rendering.
- Repositioned Device List as read-only Device Status to reduce overlap with BMS/PCS Control pages.
