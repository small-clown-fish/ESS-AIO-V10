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

# ESS-AIO v9.9.19 LTS UI Usability Fix

This release keeps the Web-Only Lite packaging line and fixes UI usability issues reported after v9.9.18.

## Fixed

- Cluster Binding can now clear a configured Power Map directly and persist it.
- Analyzer pages now include direct upload controls inside Modbus / CAN / Joint analysis tabs, not only in the Upload tab.
- Analyzer layout is less cramped while still using lightweight static HTML/CSS/JS.
- Curves CSV playback now has a file upload entry again.

## Packaging

Default packaging remains Web-Only Lite: `ESS-AIO-Web.exe`, `ESS-AIO-Runtime.exe`, and `ESS-AIO-Shutdown.exe`.

---

# ESS-AIO v9.9.18 Web-Only Lite

This release changes the default Windows packaging target to Web-Only Lite.

## Included in default artifact

- `ESS-AIO-Web.exe`
- `ESS-AIO-Runtime.exe`
- `ESS-AIO-Shutdown.exe`

## Not included by default

- `ESS-AIO.exe` Classic PySide UI
- `ESS-AIO-Launcher.exe` Classic UI + Web launcher

Build Classic UI explicitly with:

```powershell
python build_pyinstaller.py --full
```

## Runtime shutdown

Closing the browser tab does not stop Runtime. Use Web shutdown, `/shutdown`, or `ESS-AIO-Shutdown.exe`.


## v9.9.21 LTS Action Click Target Fix

Fixed dynamic Project / Cluster Binding action buttons where Edit, Clear Power Map, Remove, Add, and Save could appear clickable but do nothing. The root cause was fragile inline/dynamic event handling when the click target was a text node or the generated onclick attribute contained quoted device/cluster names. Dynamic action buttons now use safer encoded arguments and a robust event dispatcher.
