from __future__ import annotations

"""PyInstaller build script for ESS-AIO Web-Only Lite.

Default Windows field package is now a *single user-facing folder*:

    dist/ESS-AIO-Web-Lite/
        ESS-AIO-Web.exe
        ESS-AIO-Shutdown.exe
        ESS-AIO-Runtime/
            ESS-AIO-Runtime.exe
            _internal/

Why this layout:
- Runtime is large and needs its own PyInstaller onedir _internal folder.
- Web and Shutdown are small onefile launchers at the top level.
- Users only need to double-click ESS-AIO-Web.exe.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
LITE_DIR = DIST / "ESS-AIO-Web-Lite"

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
    "python_multipart",
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


def _pyinstaller(
    entry: str,
    name: str,
    hidden_imports: list[str],
    *,
    windowed: bool = True,
    onefile: bool = False,
    add_data: bool = True,
) -> None:
    if not (ROOT / entry).exists():
        print(f"[BUILD] Skip {name}: missing {entry}")
        return
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
    ]
    if windowed:
        cmd.append("--windowed")
    cmd.extend(["--name", name])
    cmd.extend(_hidden_import_args(hidden_imports))
    if add_data:
        cmd.extend(_add_data_args())
    cmd.append(entry)
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


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _assemble_lite() -> None:
    if LITE_DIR.exists():
        shutil.rmtree(LITE_DIR)
    LITE_DIR.mkdir(parents=True, exist_ok=True)

    runtime_dir = DIST / "ESS-AIO-Runtime"
    runtime_exe = runtime_dir / _exe_name("ESS-AIO-Runtime")
    web_exe = DIST / _exe_name("ESS-AIO-Web")
    shutdown_exe = DIST / _exe_name("ESS-AIO-Shutdown")

    missing = [str(p) for p in [runtime_exe, web_exe, shutdown_exe] if not p.exists()]
    if missing:
        raise SystemExit("Missing build outputs before assembly:\n" + "\n".join(missing))

    _copytree(runtime_dir, LITE_DIR / "ESS-AIO-Runtime")
    _copy_file(web_exe, LITE_DIR / _exe_name("ESS-AIO-Web"))
    _copy_file(shutdown_exe, LITE_DIR / _exe_name("ESS-AIO-Shutdown"))

    for doc in ["README.md", "CHANGELOG.md", "RELEASE_NOTES.md", "ROADMAP.md", "PACKAGING_WINDOWS.md"]:
        p = ROOT / doc
        if p.exists():
            _copy_file(p, LITE_DIR / doc)

    # Operator helper for users who prefer batch files.
    start_bat = LITE_DIR / "START_ESS_AIO_WEB.bat"
    stop_bat = LITE_DIR / "STOP_ESS_AIO_RUNTIME.bat"
    if os.name == "nt":
        start_bat.write_text("@echo off\r\ncd /d %~dp0\r\nstart \"\" ESS-AIO-Web.exe\r\n", encoding="utf-8")
        stop_bat.write_text("@echo off\r\ncd /d %~dp0\r\nESS-AIO-Shutdown.exe\r\npause\r\n", encoding="utf-8")


def _verify_lite() -> None:
    required = [
        LITE_DIR / _exe_name("ESS-AIO-Web"),
        LITE_DIR / _exe_name("ESS-AIO-Shutdown"),
        LITE_DIR / "ESS-AIO-Runtime" / _exe_name("ESS-AIO-Runtime"),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit("Missing Web-Lite package files:\n" + "\n".join(missing))
    if not (LITE_DIR / "ESS-AIO-Runtime").is_dir():
        raise SystemExit("Runtime directory missing in Web-Lite package")


def build_lite() -> None:
    # Runtime stays onedir so its own _internal dependencies remain intact.
    _pyinstaller("app_runtime.py", "ESS-AIO-Runtime", RUNTIME_HIDDEN_IMPORTS, onefile=False, windowed=True, add_data=True)

    # Web and Shutdown are small onefile launchers placed at the top level.
    _pyinstaller("app_web.py", "ESS-AIO-Web", [], onefile=True, windowed=True, add_data=False)
    _pyinstaller("app_shutdown.py", "ESS-AIO-Shutdown", [], onefile=True, windowed=True, add_data=False)

    _assemble_lite()
    _verify_lite()
    print("[BUILD] Web-Only Lite OK.")
    print("[BUILD] Start with:", LITE_DIR / _exe_name("ESS-AIO-Web"))
    print("[BUILD] Emergency stop:", LITE_DIR / _exe_name("ESS-AIO-Shutdown"))


def build_full() -> None:
    build_lite()
    _pyinstaller("app.py", "ESS-AIO", CLASSIC_HIDDEN_IMPORTS, onefile=False, windowed=True, add_data=True)
    _pyinstaller("app_launcher.py", "ESS-AIO-Launcher", [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtNetwork",
    ], onefile=True, windowed=True, add_data=False)
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
