from __future__ import annotations

"""ESS-AIO Runtime shutdown helper.

Use this when ESS-AIO-Web opened the browser and the browser page is frozen or
already closed. It calls the Runtime shutdown API and then waits briefly for the
port to go offline. Closing a browser tab never stops Runtime by itself.
"""

import json
import os
import sys
import time
import urllib.request
from urllib.error import URLError

DEFAULT_RUNTIME_URL = os.environ.get("ESS_AIO_RUNTIME_URL", "http://127.0.0.1:8765").rstrip("/")


def _post_shutdown(runtime_url: str) -> dict:
    payload = json.dumps({"source": "ESS-AIO-Shutdown", "confirmed": True}).encode("utf-8")
    req = urllib.request.Request(
        runtime_url + "/api/runtime/shutdown",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {"ok": True}


def _health_ok(runtime_url: str) -> bool:
    try:
        with urllib.request.urlopen(runtime_url + "/api/health", timeout=0.5) as resp:
            return bool(resp.status < 500)
    except Exception:
        return False


def main() -> int:
    runtime_url = os.environ.get("ESS_AIO_RUNTIME_URL", DEFAULT_RUNTIME_URL).rstrip("/")
    print(f"[SHUTDOWN] Target Runtime: {runtime_url}")
    try:
        result = _post_shutdown(runtime_url)
        print("[SHUTDOWN]", json.dumps(result, indent=2, ensure_ascii=False))
    except URLError as exc:
        print(f"[SHUTDOWN] Runtime is not reachable: {exc}")
        return 0
    except Exception as exc:
        print(f"[SHUTDOWN] Failed to request shutdown: {exc}")
        return 1

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not _health_ok(runtime_url):
            print("[SHUTDOWN] Runtime is offline.")
            return 0
        time.sleep(0.5)
    print("[SHUTDOWN] Runtime still responds. It may still be flushing state; check Task Manager if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
