from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

DATA_ITEMS = [
    ("bms_logger/protocols", "bms_logger/protocols"),
    ("bms_profiles", "bms_profiles"),
    ("pcs_profiles", "pcs_profiles"),
    ("protocols/pcs", "protocols/pcs"),
    ("config_templates", "config_templates"),
    ("default_configs", "default_configs"),
    ("profiles/default", "profiles/default"),
    ("templates", "templates"),
    ("alarm_map.json", "."),
    ("pcs_config.json", "."),
    ("runtime_config.json", "."),
    ("site_config.json", "."),
    ("devices.sample.json", "."),
    ("PCS_PROFILE_GUIDE.md", "."),
    ("CLUSTER_POWER_MAP_GUIDE.md", "."),
    ("README.md", "."),
    ("CHANGELOG.md", "."),
    ("RELEASE_NOTES.md", "."),
    ("ROADMAP.md", "."),
]

COMMON_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "pymodbus",
    "pymodbus.client",
    "pymodbus.exceptions",
    "openpyxl",
]

RUNTIME_HIDDEN_IMPORTS = COMMON_HIDDEN_IMPORTS + [
    "fastapi",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "pydantic",
    "requests",
]

LAUNCHER_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
]


def _data_sep() -> str:
    return ";" if os.name == "nt" else ":"


def _add_data_args() -> list[str]:
    args: list[str] = []
    sep = _data_sep()
    for src, dst in DATA_ITEMS:
        p = ROOT / src
        if p.exists():
            args.extend(["--add-data", f"{src}{sep}{dst}"])
    return args


def _hidden_import_args(imports: list[str]) -> list[str]:
    args: list[str] = []
    for name in imports:
        args.extend(["--hidden-import", name])
    return args


def _pyinstaller(entry: str, name: str, hidden_imports: list[str]) -> None:
    if not (ROOT / entry).exists():
        print(f"[BUILD] Skip {name}: missing {entry}")
        return
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        name,
        *_hidden_import_args(hidden_imports),
        *_add_data_args(),
        entry,
    ]
    print("[BUILD]", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def main() -> int:
    for path in [DIST, BUILD]:
        if path.exists():
            shutil.rmtree(path)
    for spec in ROOT.glob("*.spec"):
        try:
            spec.unlink()
        except Exception:
            pass

    _pyinstaller("app.py", "ESS-AIO", COMMON_HIDDEN_IMPORTS)
    _pyinstaller("app_runtime.py", "ESS-AIO-Runtime", RUNTIME_HIDDEN_IMPORTS)
    _pyinstaller("app_launcher.py", "ESS-AIO-Launcher", LAUNCHER_HIDDEN_IMPORTS)
    _pyinstaller("app_web.py", "ESS-AIO-Web", LAUNCHER_HIDDEN_IMPORTS)
    _pyinstaller("app_shutdown.py", "ESS-AIO-Shutdown", LAUNCHER_HIDDEN_IMPORTS)

    required = [
        DIST / "ESS-AIO" / ("ESS-AIO.exe" if os.name == "nt" else "ESS-AIO"),
        DIST / "ESS-AIO-Runtime" / ("ESS-AIO-Runtime.exe" if os.name == "nt" else "ESS-AIO-Runtime"),
        DIST / "ESS-AIO-Launcher" / ("ESS-AIO-Launcher.exe" if os.name == "nt" else "ESS-AIO-Launcher"),
        DIST / "ESS-AIO-Web" / ("ESS-AIO-Web.exe" if os.name == "nt" else "ESS-AIO-Web"),
        DIST / "ESS-AIO-Shutdown" / ("ESS-AIO-Shutdown.exe" if os.name == "nt" else "ESS-AIO-Shutdown"),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit("Missing build outputs:\n" + "\n".join(missing))
    print("[BUILD] OK. Start with dist/ESS-AIO-Web/ESS-AIO-Web.exe for Web-only mode. Use dist/ESS-AIO-Shutdown/ESS-AIO-Shutdown.exe if the browser is closed or frozen. Use dist/ESS-AIO-Launcher/ESS-AIO-Launcher.exe for Classic UI + Web.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
