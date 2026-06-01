from __future__ import annotations

"""ESS-AIO Web-only launcher.

Starts the headless Runtime process and opens the default browser.  It does not
start the PySide Classic UI.  Use this as the normal field entry point once the
Web EMS pages are preferred; keep ESS-AIO-Launcher for Classic UI + Web mode.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
import traceback
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_RUNTIME_URL = os.environ.get("ESS_AIO_RUNTIME_URL", "http://127.0.0.1:8765").rstrip("/")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _python_executable() -> str:
    return sys.executable or "python"


def _runtime_port_from_url(runtime_url: str) -> str:
    try:
        return str(urlparse(runtime_url).port or 8765)
    except Exception:
        return "8765"


def _health(runtime_url: str, timeout_s: float = 0.7) -> dict:
    try:
        with urllib.request.urlopen(runtime_url + "/api/health", timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _schema_ok(info: dict) -> bool:
    """Accept any healthy ESS-AIO Runtime health response.

    Older 9.x Runtime builds may report api_schema values such as
    ``9.9.21-lts-inline-action-attr-fix`` that do not contain the words
    "runtime" or "web". The Web launcher must not reject those healthy local
    runtimes; api_schema is diagnostic only.
    """
    if not isinstance(info, dict):
        return False
    if not bool(info.get("ok")):
        return False
    service = str(info.get("service", ""))
    if service and "ESS-AIO Runtime" not in service:
        return False
    return True


def _log_dir() -> Path:
    base = _base_dir()
    path = base / "logs"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path


def _notify_error(title: str, message: str) -> None:
    print(f"[WEB][ERROR] {title}: {message}")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        except Exception:
            pass


def _runtime_candidates(base: Path) -> list[Path]:
    exe = "ESS-AIO-Runtime.exe" if os.name == "nt" else "ESS-AIO-Runtime"
    return [
        base / exe,
        base / "ESS-AIO-Runtime" / exe,
        base.parent / "ESS-AIO-Runtime" / exe,
        base.parent / exe,
    ]


def _start_runtime(runtime_url: str) -> subprocess.Popen | None:
    info = _health(runtime_url)
    if _schema_ok(info):
        print(f"[WEB] Runtime already running at {runtime_url}: {info}")
        return None

    base = _base_dir()
    env = os.environ.copy()
    env["ESS_AIO_RUNTIME_URL"] = runtime_url
    env["ESS_AIO_RUNTIME_PORT"] = _runtime_port_from_url(runtime_url)

    if _is_frozen():
        candidates = _runtime_candidates(base)
        runtime_exe = next((p for p in candidates if p.exists()), None)
        if runtime_exe is None:
            searched = "\n".join(str(p) for p in candidates)
            raise FileNotFoundError(
                "Cannot find ESS-AIO-Runtime.exe.\n\nSearched:\n" + searched
            )
        cmd = [str(runtime_exe)]
        cwd = runtime_exe.parent
    else:
        cmd = [_python_executable(), str(base / "app_runtime.py")]
        cwd = base

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    log_dir = _log_dir()
    runtime_log = log_dir / "runtime_stdout.log"
    runtime_err = log_dir / "runtime_stderr.log"
    print("[WEB] Starting Runtime:", " ".join(cmd))
    print("[WEB] Runtime cwd:", cwd)
    stdout = open(runtime_log, "a", encoding="utf-8", errors="replace")
    stderr = open(runtime_err, "a", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, creationflags=creationflags, stdout=stdout, stderr=stderr)

    deadline = time.time() + 30.0
    last_info: dict = {}
    while time.time() < deadline:
        if proc.poll() is not None:
            msg = (
                f"Runtime exited early with code {proc.returncode}.\n"
                f"Check logs:\n{runtime_log}\n{runtime_err}"
            )
            _notify_error("ESS-AIO Runtime failed to start", msg)
            return proc
        last_info = _health(runtime_url)
        if _schema_ok(last_info):
            print(f"[WEB] Runtime ready at {runtime_url}")
            return proc
        time.sleep(0.5)

    msg = (
        f"Runtime did not pass health check at {runtime_url}.\n"
        f"Last health: {last_info}\n"
        f"Check logs:\n{runtime_log}\n{runtime_err}"
    )
    _notify_error("ESS-AIO Runtime not reachable", msg)
    return proc


def _shutdown_runtime(runtime_url: str) -> dict:
    try:
        req = urllib.request.Request(runtime_url + "/api/runtime/shutdown", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    runtime_url = os.environ.get("ESS_AIO_RUNTIME_URL", DEFAULT_RUNTIME_URL).rstrip("/")
    proc = _start_runtime(runtime_url)
    print(f"[WEB] Opening {runtime_url}")
    try:
        webbrowser.open(runtime_url)
    except Exception as exc:
        print(f"[WEB] Could not open browser automatically: {exc}")
    # Default Web-only mode returns immediately after opening the browser.
    # The Runtime remains available until the user clicks Web Runtime -> Shutdown,
    # opens /shutdown, runs ESS-AIO-Shutdown.exe, or closes it from Task Manager.
    # Closing the browser tab does NOT stop Runtime. Set ESS_AIO_WEB_WAIT=1 for
    # console/developer mode where this launcher waits and Ctrl+C shuts down.
    wait = os.environ.get("ESS_AIO_WEB_WAIT", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not wait:
        return 0
    print("[WEB] Developer wait mode. Press Ctrl+C to stop the Runtime started by Web mode.")
    try:
        while True:
            time.sleep(1.0)
            if proc is not None and proc.poll() is not None:
                print(f"[WEB] Runtime exited with code {proc.returncode}")
                return int(proc.returncode or 0)
    except KeyboardInterrupt:
        print("[WEB] Stopping Runtime...")
        if proc is not None:
            print("[WEB]", _shutdown_runtime(runtime_url))
            try:
                proc.wait(timeout=6.0)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            log = _log_dir() / "web_launcher_error.log"
            log.write_text(detail, encoding="utf-8")
            _notify_error("ESS-AIO Web launcher error", f"{exc}\n\nLog: {log}")
        except Exception:
            print(detail)
        raise
