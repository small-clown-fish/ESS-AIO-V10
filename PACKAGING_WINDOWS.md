# ESS-AIO Windows Packaging — Web-Only Lite

Current package: **v9.9.18 Web-Only Lite**

## Default build

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
python build_pyinstaller.py
```

The default build intentionally produces only:

- `dist/ESS-AIO-Web/ESS-AIO-Web.exe` — normal field entry point
- `dist/ESS-AIO-Runtime/ESS-AIO-Runtime.exe` — headless Runtime + Web EMS API
- `dist/ESS-AIO-Shutdown/ESS-AIO-Shutdown.exe` — emergency Runtime stop helper

It does **not** build or package the Classic PySide UI by default:

- no `ESS-AIO.exe`
- no `ESS-AIO-Launcher.exe`

This keeps the field artifact smaller and avoids running both Classic UI and Web UI at the same time.

## Normal launch

Start:

```text
ESS-AIO-Web/ESS-AIO-Web.exe
```

It will start Runtime if needed and open:

```text
http://127.0.0.1:8765
```

Closing the browser tab does **not** stop Runtime. Use one of these:

1. Web EMS → Runtime → Shutdown Runtime
2. `http://127.0.0.1:8765/shutdown`
3. `ESS-AIO-Shutdown/ESS-AIO-Shutdown.exe`
4. Windows Task Manager → end `ESS-AIO-Runtime.exe`

## Full Classic UI build, only when needed

Classic UI is no longer part of the default package. Build it explicitly:

```powershell
python build_pyinstaller.py --full
```

or:

```powershell
$env:ESS_AIO_BUILD_FULL="1"
python build_pyinstaller.py
```

The full build additionally creates:

- `dist/ESS-AIO/ESS-AIO.exe`
- `dist/ESS-AIO-Launcher/ESS-AIO-Launcher.exe`

## Size note

Runtime currently still depends on PySide6 internally because the headless Runtime reuses the existing Qt/MainWindow worker host. Therefore Web-Only Lite removes the extra Classic UI executables, but PySide6 is still included in Runtime until the Runtime core is fully decoupled from Qt.


## v9.9.23 Web-Only Lite layout

The default build creates:

```text
 dist/ESS-AIO-Web-Lite/
   ESS-AIO-Web.exe
   ESS-AIO-Shutdown.exe
   ESS-AIO-Runtime/
     ESS-AIO-Runtime.exe
     _internal/
```

Start by double-clicking `ESS-AIO-Web.exe`. Do not move it away from the folder.
