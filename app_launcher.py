from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_RUNTIME_URL = os.environ.get("ESS_AIO_RUNTIME_URL", "http://127.0.0.1:8765").rstrip("/")
REQUIRED_API_SCHEMA = "9.9.17-lts-analyzer-subpages-ui"
WATCHDOG_ENABLED = os.environ.get("ESS_AIO_RUNTIME_WATCHDOG", "1").strip().lower() not in {"0", "false", "no", "off"}
WATCHDOG_INTERVAL_S = float(os.environ.get("ESS_AIO_RUNTIME_WATCHDOG_INTERVAL", "5"))


def _python_executable() -> str:
    return sys.executable or "python"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _health(runtime_url: str = DEFAULT_RUNTIME_URL, timeout_s: float = 0.5) -> dict:
    try:
        with urllib.request.urlopen(runtime_url + "/api/health", timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _schema_ok(info: dict) -> bool:
    """Return True when a local runtime is healthy enough for the UI.

    v8.x evolves the Web/API schema frequently.  The previous launcher compared
    the schema with one old hard-coded value, so a perfectly healthy newer
    runtime returned {ok: true} but was still treated as failed by the watchdog.
    For the fixed local port launcher, an explicit ok=True health response from
    ESS-AIO Runtime is the compatibility contract; api_schema is now reported
    for diagnostics instead of used as a strict blocker.
    """
    if not isinstance(info, dict):
        return False
    if not bool(info.get("ok")):
        return False
    service = str(info.get("service", ""))
    schema = str(info.get("api_schema", ""))
    if service and "ESS-AIO Runtime" not in service:
        return False
    if schema and not any(token in schema for token in ("runtime", "web", "ESS-AIO")):
        # Be conservative if a foreign service happens to expose /api/health.
        return False
    return True


def _url_for_port(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _pick_runtime_url(preferred_url: str) -> str:
    """Use one fixed runtime URL.

    v5.5 deliberately stops auto-jumping from 8765 to 8766/8767.
    Auto port switching made it too easy for the UI to talk to one runtime while
    another runtime was controlling devices.  If the preferred port is occupied
    by an incompatible runtime, the launcher exits with a clear error.
    """
    info = _health(preferred_url)
    if not info.get("ok") or _schema_ok(info):
        return preferred_url
    raise RuntimeError(
        f"Incompatible ESS-AIO runtime already running at {preferred_url}: {info}. "
        "Stop old app_runtime.py / ESS-AIO-Runtime.exe first."
    )

def _runtime_port_from_url(runtime_url: str) -> str:
    try:
        return str(urlparse(runtime_url).port or 8765)
    except Exception:
        return "8765"


def _start_runtime(runtime_url: str = DEFAULT_RUNTIME_URL) -> subprocess.Popen | None:
    info = _health(runtime_url)
    if _schema_ok(info):
        print(f"[LAUNCHER] Compatible runtime already running at {runtime_url}")
        return None
    if info.get("ok") and not _schema_ok(info):
        raise RuntimeError(f"Runtime at {runtime_url} is incompatible; stop it first: {info}")

    base = _base_dir()
    env = os.environ.copy()
    env["ESS_AIO_RUNTIME_URL"] = runtime_url
    env["ESS_AIO_RUNTIME_PORT"] = _runtime_port_from_url(runtime_url)

    if _is_frozen():
        candidates = [
            base.parent / "ESS-AIO-Runtime" / "ESS-AIO-Runtime.exe",
            base / "ESS-AIO-Runtime.exe",
        ]
        runtime_exe = next((p for p in candidates if p.exists()), None)
        if runtime_exe is None:
            raise FileNotFoundError("Cannot find ESS-AIO-Runtime.exe next to launcher/UI artifact")
        cmd = [str(runtime_exe)]
    else:
        cmd = [_python_executable(), str(base / "app_runtime.py")]

    print("[LAUNCHER] Starting runtime:", " ".join(cmd), "URL=", runtime_url)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, cwd=str(base), env=env, creationflags=creationflags)

    deadline = time.time() + 20.0
    while time.time() < deadline:
        info = _health(runtime_url, timeout_s=0.8)
        if _schema_ok(info):
            print(f"[LAUNCHER] Runtime ready at {runtime_url}")
            return proc
        time.sleep(0.5)
    print("[LAUNCHER] Runtime did not answer compatible health check within 20s; UI will still start.")
    return proc



def _runtime_alive(runtime_url: str) -> bool:
    return _schema_ok(_health(runtime_url, timeout_s=1.0))


def _shutdown_runtime(runtime_url: str, *, timeout_s: float = 2.0) -> dict:
    try:
        req = urllib.request.Request(runtime_url + "/api/runtime/shutdown", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data) if data else {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def _terminate_runtime_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _watchdog_loop(runtime_url: str, runtime_proc_ref: dict[str, subprocess.Popen | None], stop_event: threading.Event) -> None:
    """Keep the runtime process alive while the UI launched by this launcher is running.

    This is intentionally conservative: it only restarts when the health check is
    unreachable or incompatible and the launcher can start a new runtime on the
    same fixed port. It does not jump to 8766/8767 because that made UI/runtime
    mismatches hard to diagnose in the field.
    """
    failures = 0
    while not stop_event.wait(max(1.0, WATCHDOG_INTERVAL_S)):
        info = _health(runtime_url, timeout_s=1.0)
        if _schema_ok(info):
            failures = 0
            continue
        failures += 1
        print(f"[WATCHDOG] Runtime health failed ({failures}): {info}")
        if failures < 2:
            continue
        try:
            proc = runtime_proc_ref.get("proc")
            if proc is not None and proc.poll() is None:
                print("[WATCHDOG] Runtime process handle still alive but health failed; not starting duplicate yet.")
                continue
            print("[WATCHDOG] Runtime appears down; restarting...")
            runtime_proc_ref["proc"] = _start_runtime(runtime_url)
            failures = 0
        except Exception as exc:
            print(f"[WATCHDOG] Runtime restart failed: {exc}")

def _start_ui(runtime_url: str = DEFAULT_RUNTIME_URL) -> int:
    base = _base_dir()
    env = os.environ.copy()
    env["ESS_AIO_USE_RUNTIME_API"] = "1"
    env["ESS_AIO_RUNTIME_URL"] = runtime_url

    if _is_frozen():
        candidates = [base / "ESS-AIO.exe", base.parent / "ESS-AIO" / "ESS-AIO.exe"]
        ui_exe = next((p for p in candidates if p.exists() and p.resolve() != Path(sys.executable).resolve()), None)
        if ui_exe is None:
            raise FileNotFoundError("Cannot find ESS-AIO.exe next to launcher artifact")
        cmd = [str(ui_exe)]
    else:
        cmd = [_python_executable(), str(base / "app.py")]

    print("[LAUNCHER] Starting UI:", " ".join(cmd), "URL=", runtime_url)
    return subprocess.call(cmd, cwd=str(base), env=env)


def main() -> int:
    preferred_url = os.environ.get("ESS_AIO_RUNTIME_URL", DEFAULT_RUNTIME_URL).rstrip("/")
    runtime_url = _pick_runtime_url(preferred_url)
    runtime_proc_ref: dict[str, subprocess.Popen | None] = {"proc": _start_runtime(runtime_url)}
    stop_event = threading.Event()
    monitor: threading.Thread | None = None
    if WATCHDOG_ENABLED:
        monitor = threading.Thread(target=_watchdog_loop, args=(runtime_url, runtime_proc_ref, stop_event), daemon=True, name="ESS-AIO-Runtime-Watchdog")
        monitor.start()
        print(f"[WATCHDOG] Enabled for {runtime_url}; interval={WATCHDOG_INTERVAL_S}s")
    try:
        return int(_start_ui(runtime_url) or 0)
    finally:
        stop_event.set()
        if monitor is not None:
            try:
                monitor.join(timeout=2.0)
            except Exception:
                pass
        # v9.2 lifecycle: when this launcher started a dedicated Runtime for the UI,
        # close it on UI exit by default so field laptops do not keep stale Runtime
        # processes alive. Set ESS_AIO_KEEP_RUNTIME_AFTER_UI_EXIT=1 to leave it running.
        keep = os.environ.get("ESS_AIO_KEEP_RUNTIME_AFTER_UI_EXIT", "0").strip().lower() in {"1", "true", "yes", "on"}
        proc = runtime_proc_ref.get("proc")
        if proc is not None and not keep:
            print("[LAUNCHER] UI exited; stopping Runtime that was started by this launcher...")
            result = _shutdown_runtime(runtime_url)
            print(f"[LAUNCHER] Runtime shutdown result: {result}")
            deadline = time.time() + 6.0
            while time.time() < deadline and _runtime_alive(runtime_url):
                time.sleep(0.5)
            _terminate_runtime_proc(proc)


if __name__ == "__main__":
    raise SystemExit(main())
