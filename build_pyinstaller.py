from __future__ import annotations

"""PyInstaller build script for ESS-AIO Web-Only Lite.

Default output is the field package:

    ESS-AIO-Web        -> starts Runtime and opens browser
    ESS-AIO-Runtime    -> headless runtime / API / Web EMS
    ESS-AIO-Shutdown   -> emergency shutdown helper

Classic PySide UI and Classic Launcher are intentionally excluded from the
normal build to reduce Windows artifact size and avoid starting an extra UI.
Use --full only when you explicitly need the old Classic UI package.
"""

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
    ("PACKAGING_WINDOWS.md", "."),
]

# Runtime still embeds the existing Qt-based MainWindow object as the worker
# host, so PySide6 is currently required by Runtime. Web/Shutdown launchers do
# not need PySide hidden imports.
RUNTIME_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "pymodbus",
    "pymodbus.client",
    "pymodbus.exceptions",
    "openpyxl",
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
    "multipart",
]

CLASSIC_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "pymodbus",
    "pymodbus.client",
    "pymodbus.exceptions",
    "openpyxl",
]


def _exe_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


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


def _pyinstaller(entry: str, name: str, hidden_imports: list[str], *, windowed: bool = True) -> None:
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
    ]
    if windowed:
        cmd.append("--windowed")
    cmd.extend([
        "--name",
        name,
        *_hidden_import_args(hidden_imports),
        *_add_data_args(),
        entry,
    ])
    print("[BUILD]", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def _clean() -> None:
    for path in [DIST, BUILD]:
        if path.exists():
            shutil.rmtree(path)
    for spec in ROOT.glob("*.spec"):
        try:
            spec.unlink()
        except Exception:
            pass


def _verify(names: list[str]) -> None:
    missing: list[str] = []
    for name in names:
        exe = DIST / name / _exe_name(name)
        if not exe.exists():
            missing.append(str(exe))
    if missing:
        raise SystemExit("Missing build outputs:\n" + "\n".join(missing))


def build_lite() -> None:
    _pyinstaller("app_runtime.py", "ESS-AIO-Runtime", RUNTIME_HIDDEN_IMPORTS)
    _pyinstaller("app_web.py", "ESS-AIO-Web", [])
    _pyinstaller("app_shutdown.py", "ESS-AIO-Shutdown", [])
    _verify(["ESS-AIO-Runtime", "ESS-AIO-Web", "ESS-AIO-Shutdown"])
    print("[BUILD] Web-Only Lite OK.")
    print("[BUILD] Start with dist/ESS-AIO-Web/" + _exe_name("ESS-AIO-Web"))
    print("[BUILD] Emergency stop: dist/ESS-AIO-Shutdown/" + _exe_name("ESS-AIO-Shutdown"))


def build_full() -> None:
    build_lite()
    _pyinstaller("app.py", "ESS-AIO", CLASSIC_HIDDEN_IMPORTS)
    _pyinstaller("app_launcher.py", "ESS-AIO-Launcher", [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtNetwork",
    ])
    _verify(["ESS-AIO", "ESS-AIO-Launcher"])
    print("[BUILD] Full Classic UI package OK.")


def main() -> int:
    _clean()
    full = "--full" in sys.argv or os.getenv("ESS_AIO_BUILD_FULL", "0").strip().lower() in {"1", "true", "yes", "on"}
    if full:
        build_full()
    else:
        build_lite()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
