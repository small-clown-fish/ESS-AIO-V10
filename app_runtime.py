from __future__ import annotations

import os
import sys

import json
import socket
import urllib.request
import traceback
from pathlib import Path

RUNTIME_API_SCHEMA = "9.9.24-web-lite-health-check-fix"


def _runtime_log_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home())
    else:
        base = Path.home() / ".local" / "share"
    path = base / "ESS-AIO" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _write_runtime_fatal(exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        log = _runtime_log_dir() / "runtime_fatal.log"
        log.write_text(detail, encoding="utf-8")
        print(f"[RUNTIME][FATAL] {exc}. Log: {log}")
    except Exception:
        print(detail)

def _health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/api/health"

def _runtime_health(host: str, port: int, timeout_s: float = 0.5) -> dict:
    try:
        with urllib.request.urlopen(_health_url(host, port), timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False

def _preflight_runtime_port(host: str, port: int) -> int:
    info = _runtime_health(host, port)
    if info.get("ok"):
        service = str(info.get("service", ""))
        if not service or "ESS-AIO Runtime" in service:
            print(f"[RUNTIME] Compatible runtime already running at http://{host}:{port}; not starting a second instance. info={info}")
            return 0
        print(f"[RUNTIME] Port {port} is occupied by a non ESS-AIO service: {info}")
        return 3
    if _port_open(host, port):
        print(f"[RUNTIME] Port {port} is already in use by another process. Stop it before starting ESS-AIO Runtime.")
        return 4
    return -1

from bms_logger.paths import user_data_dir
from bms_logger.release_manager import install_crash_handler

# Runtime is headless, but it still uses the existing Qt-based runtime objects in
# phase 1. Keep software rendering to avoid Windows/driver OpenGL crashes.
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ["ESS_AIO_RUNTIME_PROCESS"] = "1"
# Do not use offscreen by default on Windows; a hidden QApplication is enough.


def main() -> int:
    install_crash_handler(user_data_dir() / "logs")
    runtime_host = os.getenv("ESS_AIO_RUNTIME_HOST", "127.0.0.1")
    runtime_port = int(os.getenv("ESS_AIO_RUNTIME_PORT", "8765"))
    preflight = _preflight_runtime_port(runtime_host, runtime_port)
    if preflight >= 0:
        return preflight

    from PySide6.QtWidgets import QApplication
    from bms_logger.ui import MainWindow
    from bms_logger.runtime_api import RuntimeApiBridge, create_fastapi_app, run_uvicorn_in_thread

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    # The runtime process must always use local workers.  If the shared
    # runtime_config enables UI client mode, disable it here to avoid API
    # commands recursively calling the runtime API again.
    window.runtime_api_enabled = False
    try:
        window._apply_runtime_api_mode()
    except Exception:
        pass
    window.setWindowTitle("ESS-AIO Runtime (headless)")
    window.hide()

    bridge = RuntimeApiBridge(window)
    api_app = create_fastapi_app(bridge)
    run_uvicorn_in_thread(api_app, host=runtime_host, port=runtime_port)

    try:
        window.control_log(f"[RUNTIME] ESS-AIO runtime API started at http://{runtime_host}:{runtime_port}")
    except Exception:
        pass
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _write_runtime_fatal(exc)
        raise
