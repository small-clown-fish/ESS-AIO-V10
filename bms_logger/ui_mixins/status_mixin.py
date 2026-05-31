from __future__ import annotations

import json
import csv
from collections import deque
from pathlib import Path
from typing import Any, Dict

from PySide6.QtCharts import QChart, QLineSeries
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTableWidgetItem, QInputDialog
from PySide6.QtGui import QColor





class StatusMixin:
    def refresh_global_status_bar(self) -> None:
        if not hasattr(self, "status_selected_device_label"):
            return
        selected_device = self.current_control_device or "-"
        self.status_selected_device_label.setText(selected_device)
        self.status_sampling_label.setText(self.last_sampling_status)
        self.status_heartbeat_label.setText(self.last_heartbeat_status)
        self.status_hv_label.setText(self.last_hv_status)
        self.status_pcs_label.setText(
            "Loaded" if self.pcs_config.get("enabled", False) else "Disabled / Missing"
        )
        self.status_last_error_label.setText(self.last_error_message)

        active_cutoff = []
        for dev_name, states in self.cutoff_alarm_states.items():
            if states.get("charge_cutoff", False):
                active_cutoff.append(f"{dev_name}: charge")
            if states.get("discharge_cutoff", False):
                active_cutoff.append(f"{dev_name}: discharge")

        self.status_cutoff_label.setText("; ".join(active_cutoff) if active_cutoff else "Normal")
        self._set_label_status_color(self.status_sampling_label, self.last_sampling_status)
        self._set_label_status_color(self.status_heartbeat_label, self.last_heartbeat_status)
        self._set_label_status_color(self.status_hv_label, self.last_hv_status)
        self._set_label_status_color(self.status_pcs_label, self.status_pcs_label.text())
        self._set_label_status_color(self.status_cutoff_label, self.status_cutoff_label.text())

        # Overview table repaint can be heavy on Windows; update it at the
        # configured status cadence and only when the page is visible.
        import time
        now = time.time()
        last = float(getattr(self, "_last_overview_status_refresh", 0.0))
        interval = float(getattr(self, "status_refresh_interval", 5.0))
        if now - last >= interval:
            self._last_overview_status_refresh = now
            self.refresh_overview()

    def _set_table_item_color(self, item: QTableWidgetItem, value: str) -> None:
        text = str(value).lower()

        if text in ["online", "running", "normal"]:
            item.setForeground(QColor("green"))
        elif text in ["stale", "scheduled", "timeout", "delay"]:
            item.setForeground(QColor("orange"))
        elif text in ["offline", "error", "failed", "active", "charge", "discharge"]:
            item.setForeground(QColor("red"))
        elif text in ["stopped", "idle"]:
            item.setForeground(QColor("gray"))

    def _set_label_status_color(self, label, value: str) -> None:
        text = str(value).lower()

        if "running" in text or text in ["online", "loaded", "success", "normal"]:
            label.setStyleSheet("color: green;")
        elif "stale" in text or "timeout" in text or "warning" in text:
            label.setStyleSheet("color: orange;")
        elif "error" in text or "failed" in text or "offline" in text or "charge" in text or "discharge" in text:
            label.setStyleSheet("color: red;")
        elif "stopped" in text or "idle" in text or "disabled" in text:
            label.setStyleSheet("color: gray;")
        else:
            label.setStyleSheet("")

    def _get_cluster_voltage_stats_for_ui(self, cluster):
        max_list = []
        min_list = []

        for dev in cluster.bms_devices:
            snapshot = self.latest_snapshots.get(dev.name)
            if not snapshot:
                continue

            try:
                max_v = float(snapshot.get("max_cell_voltage"))
                min_v = float(snapshot.get("min_cell_voltage"))
            except Exception:
                continue

            max_list.append(max_v)
            min_list.append(min_v)

        if not max_list or not min_list:
            return "-", "-"

        return max(max_list), min(min_list)

    def _set_label_text_if_changed(self, label, value: object) -> None:
        text = str(value)
        try:
            if label.text() != text:
                label.setText(text)
        except Exception:
            pass

    def refresh_overview(self) -> None:
        if not hasattr(self, "overview_total_devices_label"):
            return
        # Avoid doing overview work while the overview page is hidden. The data
        # cache still updates; the model catches up when the page becomes visible.
        if hasattr(self, "_is_main_page_visible") and not self._is_main_page_visible("Overview"):
            return

        total_devices = len(self.devices)
        running_devices = len(self.device_workers)

        online_devices = 0
        for name, row in self.device_rows.items():
            item = self.device_table.item(row, 12)
            if item is not None and item.text() == "Online":
                online_devices += 1

        self._set_label_text_if_changed(self.overview_total_devices_label, total_devices)
        self._set_label_text_if_changed(self.overview_running_devices_label, running_devices)
        self._set_label_text_if_changed(self.overview_online_devices_label, online_devices)
        self._set_label_text_if_changed(self.overview_sampling_label, self.last_sampling_status)
        self._set_label_text_if_changed(self.overview_heartbeat_label, self.last_heartbeat_status)
        self._set_label_text_if_changed(self.overview_hv_label, self.last_hv_status)

        active_cutoff = []
        for dev_name, states in self.cutoff_alarm_states.items():
            if states.get("charge_cutoff", False):
                active_cutoff.append(f"{dev_name}: charge")
            if states.get("discharge_cutoff", False):
                active_cutoff.append(f"{dev_name}: discharge")

        cutoff_text = "; ".join(active_cutoff) if active_cutoff else "Normal"
        self._set_label_text_if_changed(self.overview_cutoff_label, cutoff_text)
        self._set_label_text_if_changed(self.overview_last_error_label, self.last_error_message)

        device_rows = []
        for dev in self.devices:
            name = dev["name"]
            device_row = self.device_rows.get(name)
            snapshot = self.latest_snapshots.get(name, {})

            online = "-"
            run_state = "-"
            if device_row is not None:
                online_item = self.device_table.item(device_row, 12)
                run_item = self.device_table.item(device_row, 11)
                if online_item is not None:
                    online = online_item.text()
                if run_item is not None:
                    run_state = run_item.text()

            device_rows.append([
                name,
                online,
                run_state,
                snapshot.get("soc", "-"),
                snapshot.get("system_voltage", "-"),
                snapshot.get("system_current", "-"),
                snapshot.get("system_power", "-"),
            ])

        if hasattr(self, "overview_device_model"):
            self.overview_device_model.set_rows(device_rows)
        elif hasattr(self, "overview_device_table"):
            # Legacy fallback, diff-style rather than clear/rebuild.
            self.overview_device_table.setUpdatesEnabled(False)
            try:
                self.overview_device_table.setRowCount(len(device_rows))
                for row_idx, values in enumerate(device_rows):
                    for col, value in enumerate(values):
                        text = str(value)
                        item = self.overview_device_table.item(row_idx, col)
                        if item is None:
                            item = QTableWidgetItem(text)
                            self.overview_device_table.setItem(row_idx, col, item)
                        elif item.text() != text:
                            item.setText(text)
                        if col in [1, 2]:
                            self._set_table_item_color(item, text)
            finally:
                self.overview_device_table.setUpdatesEnabled(True)

        cluster_rows = []
        if hasattr(self, "site"):
            for cluster in self.site.clusters:
                max_v, min_v = self._get_cluster_voltage_stats_for_ui(cluster)
                pcs_name = cluster.pcs_device.name if getattr(cluster, "pcs_device", None) else "-"
                derating_state = self.derating_state.get(cluster.name, {})
                cutoff_state = self.cutoff_alarm_states.get(cluster.name, {})
                derating_text = "Active" if derating_state.get("active", False) else "Normal"
                active_cutoffs = []
                if cutoff_state.get("charge_cutoff", False):
                    active_cutoffs.append("Charge")
                if cutoff_state.get("discharge_cutoff", False):
                    active_cutoffs.append("Discharge")
                cutoff_text = ", ".join(active_cutoffs) if active_cutoffs else "Normal"
                cluster_rows.append([
                    cluster.name,
                    len(cluster.bms_devices),
                    pcs_name,
                    max_v,
                    min_v,
                    derating_text,
                    cutoff_text,
                ])

        if hasattr(self, "overview_cluster_model"):
            self.overview_cluster_model.set_rows(cluster_rows)
        elif hasattr(self, "overview_cluster_table"):
            self.overview_cluster_table.setUpdatesEnabled(False)
            try:
                self.overview_cluster_table.setRowCount(len(cluster_rows))
                for row_idx, values in enumerate(cluster_rows):
                    for col, value in enumerate(values):
                        text = str(value)
                        item = self.overview_cluster_table.item(row_idx, col)
                        if item is None:
                            item = QTableWidgetItem(text)
                            self.overview_cluster_table.setItem(row_idx, col, item)
                        elif item.text() != text:
                            item.setText(text)
                        if col in [5, 6]:
                            self._set_table_item_color(item, text)
            finally:
                self.overview_cluster_table.setUpdatesEnabled(True)
