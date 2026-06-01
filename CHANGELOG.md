
## v9.9.26 LTS Dashboard + BMS Status View

- Dashboard redesigned as card + chart style site overview.
- BMS Live Devices now shows SOC, voltage, current, power, update time and friendly status labels as table columns.
- BMS status mapping: 1=Normal, 2=Full Charge, 3=Full Discharge, 4=Warning, 5=Fault.
- Raw BMS status numbers are no longer shown in the live device status column.

# Changelog

## v9.9.24 - Web Lite Health Check Fix

### Fixed
- Web launcher no longer rejects a healthy Runtime just because api_schema does not contain the words runtime/web/ESS-AIO.
- Runtime preflight now accepts any ok=True ESS-AIO Runtime on the configured local port to avoid duplicate process and false incompatibility errors.
- Classic launcher uses the same relaxed health compatibility rule.

# CHANGELOG

## v9.9.23 - Web Lite Packaging Repair

- Fixed Web-Only Lite packaging layout.
- The release ZIP now contains a single operator folder with `ESS-AIO-Web.exe`, `ESS-AIO-Shutdown.exe`, and `ESS-AIO-Runtime/ESS-AIO-Runtime.exe`.
- `ESS-AIO-Web.exe` now searches both same-folder and bundled Runtime subfolder locations.
- Runtime startup uses the Runtime folder as cwd and writes `logs/runtime_stdout.log` and `logs/runtime_stderr.log` for troubleshooting.
- Added visible error dialogs when Runtime cannot be found or fails health check.

# v9.9.22 LTS Project Config Edit Fix

- Fixed BMS Config / PCS Config Edit buttons so they visibly load the selected device into the edit form.
- Edit now scrolls/focuses the config form and supports multiple project config shapes.


## v9.9.21 - LTS UI Action Binding Fix

Fixed:
- Fixed dynamically rendered Web buttons that could appear clickable but perform no action after table re-render.
- Added a central action dispatcher for Cluster Binding / Power Map / Project device actions.
- Restored reliable Edit, Auto Even, Clear Power Map, Remove Cluster, Remove BMS/PCS, and chip remove behavior.

Checked:
- Python compile check for runtime_api.py.
- Embedded Web JavaScript syntax check with Node.

# CHANGELOG

## v9.9.19 LTS UI Usability Fix

### Fixed
- Restored a clear-and-save action for Cluster Binding Power Map, so configured weights can be removed from the row without relying on the separate Power Map page.
- Added direct file upload controls to Analyzer Modbus / CAN / Joint analysis tabs; uploaded files immediately refresh the selectors used for analysis.
- Restored Curves historical CSV upload entry and kept paste-based CSV playback as a fallback.

### Changed
- Loosened Analyzer spacing and card layout with lightweight CSS only.
- Default build remains Web-Only Lite.


## v9.9.18 Web-Only Lite

### Changed
- Default Windows build now produces only `ESS-AIO-Web`, `ESS-AIO-Runtime`, and `ESS-AIO-Shutdown`.
- Classic PySide UI and Classic Launcher are excluded from the default artifact.
- `build_pyinstaller.py --full` remains available for explicit Classic UI builds.
- GitHub Actions artifact is now `ESS-AIO-Web-Only-Lite.zip` and verifies that Classic UI outputs are not included.

### Notes
- Runtime still includes PySide6 internally because it reuses the existing Qt worker host. This version reduces package size by removing extra Classic UI executables, but a future Qt-free Runtime split is needed for deeper size reduction.


## v9.9.17 LTS Analyzer Subpages UI

- Added selected-device row highlighting in Devices, BMS Control, and PCS Control.
- Split Analyzer into Upload, Modbus, CAN Files, Joint Analysis, and History subpages.
- Analyzer uploads now immediately populate the Analysis file selectors.
- Joint Analysis now uses explicit uploaded ASC / DBC / Modbus / mapping.json selections; no default DBC is loaded.
- Kept UI changes lightweight with static CSS/HTML/JS only.


## v9.9.15 LTS Cluster Binding Remove Fix

### Fixed
- Fixed Cluster Binding row Power Map `Edit` actions when cluster names contain spaces or special characters by using safe DOM IDs and JSON-safe onclick arguments.
- Added a Power Map row-level `Remove` button next to `Edit` to clear only the current cluster power map.
- Added BMS/PCS `Remove` actions in Project device tables.
- Removing a BMS/PCS now also cleans Cluster Binding and Power Map references in site_config.

## v9.9.15 - LTS Packaging Check Final

### Fixed
- Updated stale runtime/launcher schema constants to `9.9.15-lts-cluster-binding-remove-fix`.
- Added `ESS-AIO-Shutdown.exe` verification and artifact copy in GitHub Actions.
- Added `app_shutdown.py` to the Runtime packaging self-check.
- Updated `pyproject.toml` metadata and web runtime dependencies for source installs.

### Verified
- Python syntax compilation passed for all source files.
- Embedded Web JavaScript passed `node --check`.
- JSON configuration files load successfully.

## v9.9.13 - LTS Power Map Editor Final

### Added
- Human-friendly Power Map Editor in Web Clusters / Power Map page.
- Weight table with PCS rows and BMS columns.
- Auto Even and Normalize Rows actions.

### Changed
- Power Map configuration no longer requires JSON prompt editing.
- Auto Even now normalizes each PCS row across bound BMS devices.

# Changelog

## v9.9.12 - LTS Runtime Shutdown Final

### Added
- Added a standalone `/shutdown` Web page that uses a minimal script independent of the main dashboard. Use it when the main Web UI is frozen.
- Added `app_shutdown.py` / `ESS-AIO-Shutdown.exe` helper so Windows users can stop Runtime even after closing the browser.

### Changed
- Runtime shutdown from Web now uses a browser confirmation dialog instead of manual `EXECUTE` typing.
- Build script now packages `ESS-AIO-Shutdown.exe` together with Web, Runtime, Launcher, and Classic UI.

### Clarified
- Closing the browser tab does not stop `ESS-AIO-Runtime.exe`; Runtime must be stopped through Web Shutdown, `/shutdown`, `ESS-AIO-Shutdown.exe`, Classic UI close flow, or Task Manager.

## v9.9.9 - 9.x LTS Final

### Fixed
- Aligned package version metadata with the Web Runtime schema.
- Switched high-risk Web operation prompts from manual `EXECUTE` typing to browser confirmation while preserving backend audit confirmation tokens.
- Rechecked dashboard JavaScript syntax after the Power Map fix.

### Changed
- Finalized v9.x as the Web-first LTS baseline.
- Kept documentation consolidated in `README.md`, `CHANGELOG.md`, `RELEASE_NOTES.md`, and `ROADMAP.md`.
- Release/export packaging includes the unified documentation files.

## v9.9.8 - UI Stability + Documentation Cleanup

### Fixed
- Fixed the Web dashboard JavaScript crash caused by a broken Power Map prompt string. This was the reason the page could render but buttons were not clickable.
- Kept Power Map editing in runtime-native format: `PCS -> BMS weight`.

### Changed
- Consolidated scattered patch README files into one project `README.md`.
- Added `CHANGELOG.md`, `RELEASE_NOTES.md`, and `ROADMAP.md`.
- Release Center now includes unified docs in manifest/export when present.
- High-risk Web commands use browser confirmation while preserving backend audit confirmation.

## v9.9.7 - Power Map Save Fix

- Added runtime-native nested Power Map save format.
- Added row-level Edit / Auto Even / Clear for Cluster Power Map.

## v9.9.6 - Cluster Binding Edit Lock

- Prevented Project auto-refresh from clearing in-progress Cluster Binding selections.

## v9.9.5 - Cluster Binding Save Fix

- Protected pending Cluster Binding edits before Save.

## v9.9.4 - Navigation and Binding UI

- Added icon/group style navigation.
- Reworked Cluster Binding to use Add/Remove chips.

## v9.9.3 - Modern Web UI

- Restyled Web EMS into dark EMS dashboard layout.

## v9.9 LTS

- Added 9.x LTS audit endpoints and Web page.

## v9.9.10 LTS HV Control Final

### Fixed
- Restored explicit Web BMS HV workflow buttons: selected BMS HV ON, selected BMS HV OFF, HV ON All Online, and HV OFF All Online.
- Restored the `Ignore PCS precheck / BMS-only` checkbox in the Web HV workflow panel.
- Web HV commands now pass `ignore_pcs_precheck` through the API payload for audit/future full-workflow compatibility.

### Notes
- Current runtime HV workflow remains BMS-worker based. It reuses existing BMS polling worker queues and does not start additional PCS reconnect/precheck loops.

## v9.9.11 LTS Runtime & Dispatch Final

### Added
- Web Settings / Runtime Settings table with value/source/editability/restart-required fields.
- Runtime settings API: `/api/runtime/settings` GET/POST.
- Power Map Runtime Status API: `/api/power-map/status`.
- Power Map status table showing runtime-native `PCS -> BMS weight` mapping and validation issues.

### Changed
- 9.x LTS schema updated to `9.9.11-lts-runtime-dispatch-final`.
- Power Map is now explicitly validated as runtime strategy input; full site-level Fleet EMS dispatch remains v10 scope.


## v9.9.16 LTS Analyzer Upload UI

- Analyzer 页面新增 ASC / DBC / PCAP / PCAPNG / CSV / mapping.json 浏览器上传入口。
- 默认不再加载 can_mapping_sample.dbc / can_mapping_sample.json，联合分析必须手动选择 DBC 和 mapping.json。
- 新增 Session Files 表格，可点击 Use 填入分析路径。
- 增加轻量化 Analyzer 卡片布局，不引入 React/Vue 等重型框架。
- 新增 /api/analyzer/upload 和 /api/analyzer/files。


## v9.9.21 LTS Action Click Target Fix

Fixed dynamic Project / Cluster Binding action buttons where Edit, Clear Power Map, Remove, Add, and Save could appear clickable but do nothing. The root cause was fragile inline/dynamic event handling when the click target was a text node or the generated onclick attribute contained quoted device/cluster names. Dynamic action buttons now use safer encoded arguments and a robust event dispatcher.


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
