# v9.9.22 LTS Project Config Edit Fix

- Fixed BMS Config / PCS Config Edit buttons so they visibly load the selected device into the edit form.
- Edit now scrolls/focuses the config form and supports multiple project config shapes.

# ESS-AIO

ESS-AIO is a field commissioning and Web EMS runtime toolkit for BMS/PCS/site diagnostics.

## Current line

Current package: **v9.9.19 LTS UI Usability Fix**.

## Runtime Shutdown Behavior

When using `ESS-AIO-Web.exe`, the launcher starts `ESS-AIO-Runtime.exe` and opens the browser. Closing the browser tab does **not** stop Runtime. This is intentional: the browser is only the UI, while Runtime owns polling, workers, CSV recording, strategies, and Web APIs.

Recommended shutdown options:

1. In Web EMS: open **Runtime → Shutdown Runtime**.
2. If the main Web UI is frozen: open `http://127.0.0.1:8765/shutdown`.
3. If the browser is closed or unusable: run `ESS-AIO-Shutdown.exe`.
4. Emergency fallback: end `ESS-AIO-Runtime.exe` in Windows Task Manager.


Recommended field entry:

```text
ESS-AIO-Web.exe
  -> starts ESS-AIO-Runtime.exe
  -> opens browser at http://127.0.0.1:8765
  -> does not start the Classic PySide UI
```

Classic UI is no longer included in the default Web-Only Lite artifact. Build with `python build_pyinstaller.py --full` only when Classic UI is needed.

Classic UI is retained in source as an emergency/compatibility entry through **ESS-AIO-Launcher.exe**.

## Architecture

```text
Runtime
  - owns device polling, control commands, strategies, CSV/alarm logging and snapshots

Web EMS
  - primary field UI in the browser
  - uses Runtime APIs and compact snapshots

Classic PySide UI
  - compatibility and emergency fallback
```

UI and Web should not poll equipment independently. Device IO must go through Runtime workers.

## Main Web pages

- Runtime Center
- Project / Cluster Binding / Power Map
- Devices
- BMS Control
- PCS Control
- Register Debug
- Strategy Center
- Alarm Center
- Diagnosis Workbench
- Curves
- Release Center
- Health Monitor
- UI-Web Parity
- 9.x LTS Audit

## Windows packaging

Local build:

```bash
python build_pyinstaller.py
```

Expected outputs:

```text
dist/ESS-AIO-Web/ESS-AIO-Web.exe          # preferred Web-only field entry
dist/ESS-AIO-Runtime/ESS-AIO-Runtime.exe  # headless runtime
dist/ESS-AIO-Launcher/ESS-AIO-Launcher.exe# Classic UI + Web fallback
dist/ESS-AIO/ESS-AIO.exe                  # Classic UI only
```

## 9.x LTS notes

- Web-only mode is the preferred daily field workflow.
- Project page is configuration only; connection actions belong on the control/device pages.
- Cluster Binding uses added devices as selectable sources and saves into the site configuration.
- Power Map uses runtime-native format: `PCS -> BMS weight`.
- High-risk Web commands use browser confirmation and still write audit records with backend confirmation tokens.
- The frontend JavaScript is kept as a single embedded dashboard for simple offline packaging; `node --check` should pass before release.

## Extra reference guides

- `PCS_PROFILE_GUIDE.md` - PCS profile authoring reference
- `CLUSTER_POWER_MAP_GUIDE.md` - Cluster power allocation reference
- `PACKAGING_WINDOWS.md` - Windows packaging notes

## Documentation policy

Do not create one README per patch. Keep this file as the single project README. Update:

- `CHANGELOG.md` for version history
- `RELEASE_NOTES.md` for the current release
- `ROADMAP.md` for future plan


## v9.9.10 LTS HV Control Final

Web BMS Control restores the explicit PySide-style HV workflow controls:

- Selected BMS HV ON Workflow
- Selected BMS HV OFF Workflow
- HV ON All Online
- HV OFF All Online
- Ignore PCS precheck / BMS-only checkbox

The runtime implementation remains BMS-worker based and reuses existing BMS polling queues.


## v9.9.11 LTS Notes

- Web Settings includes Runtime Settings from `runtime_config.json`.
- Power Map status validates runtime-native `PCS -> BMS weight` mappings.
- Fleet EMS dispatch remains the next major v10 milestone.

## v9.9.13 LTS Notes

- Added a human-friendly Web Power Map Editor under **Clusters / Power Map**.
- Users can configure PCS → BMS weights in table cells instead of writing JSON.
- Added Auto Even and Normalize Rows helpers.
- Power Map Runtime Status still validates whether each PCS row sums to 1.000.


## v9.9.17 LTS Analyzer Upload UI

- Analyzer 页面新增 ASC / DBC / PCAP / PCAPNG / CSV / mapping.json 浏览器上传入口。
- 默认不再加载 can_mapping_sample.dbc / can_mapping_sample.json，联合分析必须手动选择 DBC 和 mapping.json。
- 新增 Session Files 表格，可点击 Use 填入分析路径。
- 增加轻量化 Analyzer 卡片布局，不引入 React/Vue 等重型框架。
- 新增 /api/analyzer/upload 和 /api/analyzer/files。

### Current LTS patch: v9.9.21

This patch fixes Web UI action binding for dynamically rendered tables. Cluster Binding and Power Map action buttons use a central dispatcher, so Edit / Clear / Remove / Save continue to work after automatic refresh or table re-render.


## v9.9.21 LTS Action Click Target Fix

Fixed dynamic Project / Cluster Binding action buttons where Edit, Clear Power Map, Remove, Add, and Save could appear clickable but do nothing. The root cause was fragile inline/dynamic event handling when the click target was a text node or the generated onclick attribute contained quoted device/cluster names. Dynamic action buttons now use safer encoded arguments and a robust event dispatcher.
# ESS-AIO-V10
