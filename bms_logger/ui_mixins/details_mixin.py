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





class DetailsMixin:
    def refresh_details(self, device_name: str) -> None:
        if hasattr(self, "_is_main_page_visible") and not self._is_main_page_visible("Details"):
            return
        self.current_detail_device = device_name
        text = f"Current device: {device_name}"
        if self.detail_device_label.text() != text:
            self.detail_device_label.setText(text)

        snapshot = self.latest_snapshots.get(device_name, {})
        for key, _label in self.DETAIL_FIELDS:
            value = str(snapshot.get(key, "-"))
            label = self.detail_value_labels.get(key)
            if label is not None and label.text() != value:
                label.setText(value)


