from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Any

EXPECTED_API_SCHEMA = "9.2.0-runtime-lifecycle"


class RuntimeApiClient:
    """Tiny stdlib HTTP client for the local ESS-AIO runtime API.

    Uses urllib instead of requests so the Windows build does not need another
    dependency.  This client is intentionally synchronous; UI callers should use
    it only for short local API calls or from a QTimer polling loop with small
    timeouts.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout_s: float = 1.5) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout_s = float(timeout_s)

    def get(self, path: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        url = self.base_url + "/" + path.lstrip("/")
        try:
            with urllib.request.urlopen(url, timeout=float(timeout_s or self.timeout_s)) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data) if data else {"ok": True}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def post(self, path: str, payload: dict[str, Any] | None = None, *, timeout_s: float | None = None) -> dict[str, Any]:
        url = self.base_url + "/" + path.lstrip("/")
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(timeout_s or self.timeout_s)) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data) if data else {"ok": True}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # Backward-compatible retry for an old runtime API that expected a
            # required `req` query parameter.  New runtimes accept JSON bodies.
            if exc.code == 422 and ('"query","req"' in detail or '"query","request"' in detail) and payload:
                try:
                    param_name = "request" if '"query","request"' in detail else "req"
                    retry_url = url + "?" + urlencode({param_name: json.dumps(payload or {})})
                    retry_req = urllib.request.Request(retry_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(retry_req, timeout=float(timeout_s or self.timeout_s)) as resp:
                        data = resp.read().decode("utf-8", errors="replace")
                    return json.loads(data) if data else {"ok": True}
                except Exception as retry_exc:
                    return {"ok": False, "error": f"HTTP {exc.code}: {detail}; retry failed: {retry_exc}"}
            return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    def health(self) -> dict[str, Any]:
        return self.get("/api/health", timeout_s=0.8)

    def runtime_info(self) -> dict[str, Any]:
        return self.get("/api/runtime/info", timeout_s=1.0)

    def watchdog(self) -> dict[str, Any]:
        return self.get("/api/runtime/watchdog", timeout_s=1.0)


    def shutdown(self) -> dict[str, Any]:
        return self.post("/api/runtime/shutdown", {}, timeout_s=2.0)


    def site_config(self) -> dict[str, Any]:
        return self.get("/api/site/config", timeout_s=2.0)

    def update_site_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/site/config", config or {}, timeout_s=5.0)

    def save_site_config(self) -> dict[str, Any]:
        return self.post("/api/site/save", {}, timeout_s=5.0)

    def delete_cluster(self, cluster: str) -> dict[str, Any]:
        return self.post("/api/site/delete-cluster", {"cluster": cluster}, timeout_s=5.0)


    def csv_status(self) -> dict[str, Any]:
        return self.get("/api/csv/status", timeout_s=2.0)

    def start_bms_csv(self, devices: list[str]) -> dict[str, Any]:
        return self.post("/api/csv/bms/start", {"devices": list(devices or [])}, timeout_s=5.0)

    def stop_bms_csv(self, devices: list[str]) -> dict[str, Any]:
        return self.post("/api/csv/bms/stop", {"devices": list(devices or [])}, timeout_s=5.0)

    def start_pcs_csv(self, devices: list[str]) -> dict[str, Any]:
        return self.post("/api/csv/pcs/start", {"devices": list(devices or [])}, timeout_s=5.0)

    def stop_pcs_csv(self, devices: list[str]) -> dict[str, Any]:
        return self.post("/api/csv/pcs/stop", {"devices": list(devices or [])}, timeout_s=5.0)

    def logs_status(self) -> dict[str, Any]:
        return self.get("/api/logs/status", timeout_s=2.0)

    def operation_log_recent(self, max_lines: int = 300) -> dict[str, Any]:
        return self.get(f"/api/logs/operation/recent?max_lines={int(max_lines)}", timeout_s=3.0)

    def is_compatible(self) -> bool:
        info = self.health()
        if not isinstance(info, dict) or not info.get("ok"):
            return False
        service = str(info.get("service", ""))
        schema = str(info.get("api_schema", ""))
        if service and "ESS-AIO Runtime" not in service:
            return False
        if schema and not any(token in schema for token in ("runtime", "web", "ESS-AIO")):
            return False
        return True

    def device_snapshot(self, kind: str, name: str) -> dict[str, Any]:
        return self.get(f"/api/device/{kind}/{name}/snapshot", timeout_s=2.0)

    def bms_alarms(self, name: str) -> dict[str, Any]:
        return self.get(f"/api/device/bms/{name}/alarms", timeout_s=2.0)

    def separation_audit(self) -> dict[str, Any]:
        return self.get("/api/runtime/separation-audit", timeout_s=2.0)
