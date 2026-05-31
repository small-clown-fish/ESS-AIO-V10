from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from ..hv_controller import HvWorkflowController, HvWorkflowWorker
from ..modbus_client import BmsModbusClient
from ..pcs_client import PcsClient
from ..client_factory import create_bms_client, create_pcs_client
from ..worker import HeartbeatWorker


class HvControlMixin:
    def _start_hv_workflow_for_device(self, device_name: str, mode: str, source: str = "Manual") -> bool:
        """Start a single BMS HV workflow without opening reconnecting helper loops.

        Bulk HV commands use this helper with QTimer stagger.  Only devices that
        already have a running BMS polling worker are accepted, so an offline BMS
        is not probed/reconnected just because the operator pressed an all-button.
        """
        device_name = str(device_name or "").strip()
        if not device_name:
            return False
        if device_name in self.hv_workers:
            self.control_log(f"[{source}] {device_name}: HV workflow already running")
            return False
        if hasattr(self, "_get_bms_polling_worker") and self._get_bms_polling_worker(device_name) is None:
            self.control_log(f"[{source}][SKIP] {device_name}: BMS polling worker not running; HV workflow not started")
            return False

        controller = self._build_hv_controller(device_name)
        if controller is None:
            return False

        worker = HvWorkflowWorker(
            mode=mode,
            controller=controller,
            device_name=device_name,
            log_callback=self.on_hv_workflow_log,
            finished_callback=self.on_hv_workflow_finished,
        )
        self.hv_workers[device_name] = worker
        self.last_hv_status = "Running"
        self.control_state_label.setText("Executing")
        self.last_control_result_label.setText(f"HV {'ON' if mode == 'on' else 'OFF'} running")
        self.refresh_global_status_bar()
        self.control_log(f"[{source}] {device_name}: HV {'ON' if mode == 'on' else 'OFF'} workflow started")
        worker.start()
        return True

    def _bulk_start_hv_workflows(self, mode: str) -> None:
        if getattr(self, "runtime_api_enabled", False):
            mode_text = "HV ON" if mode == "on" else "HV OFF"
            reply = QMessageBox.question(self, f"Confirm {mode_text} All", f"Queue BMS-only {mode_text} sequence on runtime?")
            if reply != QMessageBox.Yes:
                return
            result = self._runtime_post("/api/bms/hv-all", {"mode": mode, "timeout": float(getattr(self, "hv_step_timeout", 30.0) or 30.0), "poll_interval": float(getattr(self, "hv_poll_interval", 1.0) or 1.0)}, timeout_s=5.0) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
            if result.get("ok"):
                self.last_control_result_label.setText(f"Runtime {mode_text} queued: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.control_log(f"[RUNTIME_UI] {mode_text} All sent to runtime: {result.get('queued', 0)}/{result.get('total', 0)}")
                self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
            else:
                QMessageBox.warning(self, "Runtime API", f"{mode_text} All failed: {result.get('error', result)}")
            return
        """Queue BMS-only HV ON/OFF sequence for all online BMS.

        The previous all-online implementation launched the legacy HV workflow,
        which creates fresh BMS/PCS clients and can silently fail or fight the
        polling worker.  Bulk operations now reuse each online BMS polling
        worker's own queue, so they do not probe offline devices and do not
        start extra reconnect loops.
        """
        names = self._online_bms_worker_names() if hasattr(self, "_online_bms_worker_names") else []
        if not names:
            QMessageBox.information(self, "BMS HV Workflow", "No online/running BMS polling workers.")
            return
        mode_text = "HV ON" if mode == "on" else "HV OFF"
        reply = QMessageBox.question(
            self,
            f"Confirm {mode_text} All",
            f"Queue BMS-only {mode_text} sequence for {len(names)} online BMS?\n\n"
            "The command will reuse each BMS polling worker queue and start staggered.",
        )
        if reply != QMessageBox.Yes:
            return

        stagger = max(0.1, float(getattr(self, "worker_start_stagger_seconds", 0.5) or 0.5))
        timeout = float(getattr(self, "hv_step_timeout", 30.0) or 30.0)
        poll = float(getattr(self, "hv_poll_interval", 1.0) or 1.0)
        method_name = "hv_on_bms_only" if mode == "on" else "hv_off_bms_only"
        queued = 0

        def queue_one(name: str) -> None:
            ok = self._enqueue_bms_worker_command(
                name,
                method_name,
                timeout,
                poll,
                label=f"{mode_text} BMS-only sequence",
            )
            if ok:
                self.control_log(f"[BMS][BULK] {name}: {mode_text} queued")
            else:
                self.control_log(f"[BMS][BULK][SKIP] {name}: polling worker not running; {mode_text} not queued")

        for index, name in enumerate(names):
            queued += 1
            QTimer.singleShot(int(index * stagger * 1000), lambda n=name: queue_one(n))

        self.control_state_label.setText("Queued" if queued else "Idle")
        self.last_control_result_label.setText(f"{mode_text} all queued: {queued}/{len(names)}")
        self.control_log(f"[BMS][BULK] {mode_text} BMS-only queued: {queued}/{len(names)}, stagger={stagger:.2f}s")

    def _runtime_queue_single_hv(self, device_name: str, mode: str) -> bool:
        if not getattr(self, "_runtime_should_handle_command", lambda: False)():
            return False
        mode_text = "HV ON" if mode == "on" else "HV OFF"
        result = self._runtime_post(
            "/api/bms/hv",
            {
                "device": device_name,
                "mode": mode,
                "timeout": float(getattr(self, "hv_step_timeout", 30.0) or 30.0),
                "poll_interval": float(getattr(self, "hv_poll_interval", 1.0) or 1.0),
            },
            timeout_s=5.0,
        ) if hasattr(self, "_runtime_post") else {"ok": False, "error": "runtime helper missing"}
        if result.get("ok"):
            self.last_control_result_label.setText(f"Runtime {mode_text} queued: {device_name}")
            self.control_log(f"[RUNTIME_UI] {mode_text} sent to runtime: {device_name}")
            self.poll_runtime_snapshot() if hasattr(self, "poll_runtime_snapshot") else None
        else:
            QMessageBox.warning(self, "Runtime API", f"{mode_text} failed: {result.get('error', result)}")
        return True

    def handle_hv_on(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return
        reply = QMessageBox.question(self, "Confirm HV On", f"Execute HV On workflow for {device_name}?")
        if reply != QMessageBox.Yes:
            return
        if self._runtime_queue_single_hv(device_name, "on"):
            return
        if device_name in self.hv_workers:
            QMessageBox.information(self, "Info", f"HV workflow already running for {device_name}")
            return
        self._start_hv_workflow_for_device(device_name, "on", source="CONTROL")

    def handle_hv_off(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return
        reply = QMessageBox.question(self, "Confirm HV Off", f"Execute HV Off workflow for {device_name}?")
        if reply != QMessageBox.Yes:
            return
        if self._runtime_queue_single_hv(device_name, "off"):
            return
        if device_name in self.hv_workers:
            QMessageBox.information(self, "Info", f"HV workflow already running for {device_name}")
            return
        self._start_hv_workflow_for_device(device_name, "off", source="CONTROL")

    def handle_hv_on_all_online_bms(self) -> None:
        self._bulk_start_hv_workflows("on")

    def handle_hv_off_all_online_bms(self) -> None:
        self._bulk_start_hv_workflows("off")

    def handle_cancel_hv_workflow(self) -> None:
        device_name = self._get_selected_control_device()
        if not device_name:
            return

        worker = self.hv_workers.get(device_name)
        if not worker:
            self.control_log(f"[CONTROL] {device_name}: No HV workflow running")
            return

        worker.stop()
        self.control_state_label.setText("Cancelling")
        self.last_control_result_label.setText("HV workflow cancelling")
        self.control_log(f"[CONTROL] {device_name}: HV workflow cancel requested")
        self.last_hv_status = "Cancelling"
        self.refresh_global_status_bar()
    def start_hv_off_for_device(self, device_name: str, source: str = "Manual") -> None:
        self._start_hv_workflow_for_device(device_name, "off", source=source)
