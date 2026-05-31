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





class SchedulerMixin:
    def on_task_status_received(self, device_name: str, status: Dict[str, Any]) -> None:
        if not hasattr(self, "task_status_store"):
            return

        prev = self.task_status_store._items.get(device_name)  # internal but simple for UI aggregation
        reads = getattr(prev, "reads", 0) if prev else 0
        errors = getattr(prev, "errors", 0) if prev else 0

        if status.get("read_ok"):
            reads += 1
        if status.get("error"):
            errors += 1

        self.task_status_store.update(
            device_name,
            status=str(status.get("status", "Idle")),
            reads=reads,
            errors=errors,
            last_latency_ms=float(status.get("last_latency_ms", 0.0) or 0.0),
            last_message=str(status.get("last_message", "-")),
        )

        # Updating task status can be very high frequency when 72 fleet workers are
        # idle/ready. Keep the data store current, but repaint the Qt table only
        # when the Scheduler/Tasks page is visible and at a low rate.
        import time
        now = time.time()
        interval = float(getattr(self, "status_refresh_interval", 5.0)) if getattr(self, "performance_mode_enabled", True) else 1.0
        last = float(getattr(self, "_last_task_status_view_refresh", 0.0))
        visible = True
        if hasattr(self, "_is_main_page_visible"):
            visible = self._is_main_page_visible("Scheduler", "Tasks", "Status")
        if visible and (now - last) >= max(1.0, interval):
            self._last_task_status_view_refresh = now
            self.refresh_task_status_view()

    def refresh_task_status_view(self) -> None:
        if not hasattr(self, "task_status_table") or not hasattr(self, "task_status_store"):
            return

        if hasattr(self, "_is_main_page_visible") and not self._is_main_page_visible("Scheduler", "Tasks", "Status"):
            return
        rows = self.task_status_store.rows()
        try:
            signature = tuple((r.get("device_name"), r.get("status"), r.get("reads"), r.get("errors"), r.get("last_message")) for r in rows)
            if signature == getattr(self, "_last_task_status_table_signature", None):
                return
            self._last_task_status_table_signature = signature
        except Exception:
            pass
        self.task_status_table.setUpdatesEnabled(False)
        self.task_status_table.setRowCount(0)

        try:
            for item in rows:
                row_idx = self.task_status_table.rowCount()
                self.task_status_table.insertRow(row_idx)
                values = [
                    item.get("device_name", "-"),
                    item.get("status", "-"),
                    str(item.get("reads", 0)),
                    str(item.get("errors", 0)),
                    str(item.get("last_latency_ms", 0.0)),
                    item.get("last_update", "-"),
                    item.get("last_message", "-"),
                ]
                for col, value in enumerate(values):
                    table_item = QTableWidgetItem(str(value))
                    if col == 1:
                        self._set_table_item_color(table_item, str(value))
                    self.task_status_table.setItem(row_idx, col, table_item)
        finally:
            self.task_status_table.setUpdatesEnabled(True)
