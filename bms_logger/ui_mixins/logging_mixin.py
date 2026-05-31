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





class LoggingMixin:
    def _should_show_log_message(self, message: str, *, interval: float = 5.0) -> bool:
        """Throttle repeated high-frequency UI log lines.

        The operation log still receives important ERROR/CUTOFF entries, but the
        QTextEdit view is protected from thousands of identical timeout lines.
        """
        import re
        import time

        noisy_patterns = (
            "Read telemetry failed",
            "Connect failed",
            "connect failed",
            "Broken pipe",
            "transaction_id",
            "No response received",
            "Heartbeat exception",
            "BMS_TIMEOUT",
            "cluster allowed power is 0",
        )
        if not (message.startswith("[ERROR]") or any(p in message for p in noisy_patterns)):
            return True
        bucket = getattr(self, "_log_throttle_state", None)
        if bucket is None:
            bucket = {}
            self._log_throttle_state = bucket
        # Normalize counters/details so repeated failures collapse together.
        key = message
        key = re.sub(r"transaction_id=\d+", "transaction_id=n", key)
        key = re.sub(r"got id=\d+", "got id=n", key)
        key = re.sub(r"\(\d+\)", "(n)", key)
        key = re.sub(r"retry=\d+s", "retry=ns", key)
        now = time.time()
        last = float(bucket.get(key, 0.0))
        if now - last < interval:
            return False
        bucket[key] = now
        return True

    def log(self, message: str) -> None:
        show_in_view = self._should_show_log_message(message)
        if hasattr(self, "log_text") and show_in_view:
            self.log_text.append(message)

            # 限制最大行数（比如 1000 行）
            doc = self.log_text.document()
            max_lines = 1000
            if doc.blockCount() > max_lines:
                cursor = self.log_text.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.select(cursor.SelectionType.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

            # 自动滚动到底部
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )

        elif show_in_view:
            print(message)

        if message.startswith("[ERROR]") or message.startswith("[CUTOFF]"):
            self.operation_log(message)

    def _should_show_control_log_message(self, message: str, *, interval: float = 5.0) -> bool:
        """Return whether a control log line should be painted in the UI.

        Windows field mode defaults to a quiet Control Log: keep warnings/errors,
        cutoff/timeout/fault, and true state transitions; suppress INFO-level
        heartbeat/queue/strategy success noise. This protects QTextEdit from
        repaint storms during 24-BMS/48-PCS runs.
        """
        import re
        import time

        text = str(message)
        lower = text.lower()

        # Always drop known high-frequency success/no-op lines from the UI.
        quiet_success_patterns = (
            "heartbeat write ok", "hb write ok", "heartbeat queued",
            "bms polling worker not running; heartbeat not queued",
            "[bms][queue] ok", "set_active_power ok", "cluster_strategy ok",
            "command worker online", "idle; no connect while idle",
        )
        if any(p in lower for p in quiet_success_patterns):
            return False

        # Show only operator-relevant state and fault lines by default.
        show_tokens = (
            "[warn]", "warning", "[error]", "error", "exception", "failed",
            "timeout", "broken pipe", "transaction_id", "no response",
            "cutoff", "fault", "offline", "recovered", "cooldown",
            "strategy started", "strategy stopped", "started", "stopped",
        )
        if not any(token in lower for token in show_tokens):
            return False

        # Rate-limit repeated noisy state/fault lines after normalization.
        bucket = getattr(self, "_control_log_throttle_state", None)
        if bucket is None:
            bucket = {}
            self._control_log_throttle_state = bucket

        key = text
        key = re.sub(r"heartbeat=\d+", "heartbeat=n", key, flags=re.IGNORECASE)
        key = re.sub(r"transaction_id=\d+", "transaction_id=n", key, flags=re.IGNORECASE)
        key = re.sub(r"got id=\d+", "got id=n", key, flags=re.IGNORECASE)
        key = re.sub(r"value=\[?\d+\]?", "value=n", key, flags=re.IGNORECASE)
        key = re.sub(r"current=[-+]?\d+(?:\.\d+)?kW", "current=nkW", key)
        key = re.sub(r"allowed=[-+]?\d+(?:\.\d+)?kW", "allowed=nkW", key)

        now = time.time()
        last = float(bucket.get(key, 0.0))
        if now - last < interval:
            return False
        bucket[key] = now
        return True

    def control_log(self, message: str) -> None:
        show_in_view = self._should_show_control_log_message(message)
        if hasattr(self, "control_log_text") and show_in_view:
            self.control_log_text.append(message)

            doc = self.control_log_text.document()
            max_lines = 1000
            if doc.blockCount() > max_lines:
                cursor = self.control_log_text.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.select(cursor.SelectionType.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

            self.control_log_text.verticalScrollBar().setValue(
                self.control_log_text.verticalScrollBar().maximum()
            )

        elif show_in_view:
            print(message)

        if show_in_view:
            self.operation_log(message)

    def operation_log(self, message: str) -> None:
        from datetime import datetime
        from pathlib import Path

        try:
            log_dir = self.get_profile_path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)

            date_str = datetime.now().strftime("%Y%m%d")
            log_path = log_dir / f"operation_{date_str}.log"

            # Size based rotation for Windows/site long-run tests. Keep 5 files of
            # about 10MB per day so an error storm cannot fill the disk or slow UI.
            max_bytes = 10 * 1024 * 1024
            backup_count = 5
            try:
                if log_path.exists() and log_path.stat().st_size >= max_bytes:
                    for index in range(backup_count - 1, 0, -1):
                        src = log_dir / f"operation_{date_str}.log.{index}"
                        dst = log_dir / f"operation_{date_str}.log.{index + 1}"
                        if src.exists():
                            if dst.exists():
                                dst.unlink()
                            src.rename(dst)
                    first = log_dir / f"operation_{date_str}.log.1"
                    if first.exists():
                        first.unlink()
                    log_path.rename(first)
            except Exception:
                pass

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{timestamp} {message}\n"

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)

        except Exception:
            pass


    # ------------------------------------------------------------------
    # Windows UI-freeze guard logging path
    # These method definitions intentionally appear later in the class and
    # override the earlier lightweight implementations above. The goal is to
    # prevent QTextEdit repaint storms and synchronous file I/O from freezing
    # the PyInstaller/Windows UI.

    def _ensure_log_flush_timer(self) -> None:
        if getattr(self, "_log_flush_timer", None) is not None:
            return
        self._pending_log_lines = []
        self._pending_control_log_lines = []
        timer = QTimer(self)
        timer.setInterval(int(getattr(self, "log_flush_interval_ms", 1000)))
        timer.timeout.connect(self._flush_text_log_queues)
        timer.start()
        self._log_flush_timer = timer

    def _append_limited_text(self, widget: Any, lines: list[str], max_lines: int = 1000) -> None:
        if not lines:
            return
        try:
            # One QTextEdit update per batch is much cheaper than append() per line.
            widget.append("\n".join(str(x) for x in lines))
            doc = widget.document()
            overflow = max(0, int(doc.blockCount()) - int(max_lines))
            if overflow:
                cursor = widget.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                for _ in range(min(overflow, 200)):
                    cursor.select(cursor.SelectionType.LineUnderCursor)
                    cursor.removeSelectedText()
                    cursor.deleteChar()
            widget.verticalScrollBar().setValue(widget.verticalScrollBar().maximum())
        except Exception:
            pass

    def _flush_text_log_queues(self) -> None:
        try:
            normal = getattr(self, "_pending_log_lines", [])
            control = getattr(self, "_pending_control_log_lines", [])
            if normal:
                self._pending_log_lines = []
                if hasattr(self, "log_text"):
                    self._append_limited_text(self.log_text, normal, max_lines=500 if getattr(self, "performance_mode_enabled", True) else 1000)
                else:
                    for line in normal:
                        print(line)
            if control:
                self._pending_control_log_lines = []
                if hasattr(self, "control_log_text"):
                    self._append_limited_text(self.control_log_text, control, max_lines=500 if getattr(self, "performance_mode_enabled", True) else 1000)
                else:
                    for line in control:
                        print(line)
        except Exception:
            pass

    def _ensure_async_operation_logger(self) -> None:
        if getattr(self, "_operation_log_queue", None) is not None:
            return
        import queue
        import threading
        self._operation_log_queue = queue.Queue(maxsize=5000)
        self._operation_log_stop = object()

        def writer() -> None:
            from datetime import datetime
            from pathlib import Path
            q = self._operation_log_queue
            stop = self._operation_log_stop
            while True:
                try:
                    item = q.get()
                    if item is stop:
                        break
                    log_dir_str, message = item
                    log_dir = Path(log_dir_str)
                    log_dir.mkdir(parents=True, exist_ok=True)
                    date_str = datetime.now().strftime("%Y%m%d")
                    log_path = log_dir / f"operation_{date_str}.log"
                    max_bytes = 10 * 1024 * 1024
                    backup_count = 5
                    try:
                        if log_path.exists() and log_path.stat().st_size >= max_bytes:
                            for index in range(backup_count - 1, 0, -1):
                                src = log_dir / f"operation_{date_str}.log.{index}"
                                dst = log_dir / f"operation_{date_str}.log.{index + 1}"
                                if src.exists():
                                    if dst.exists():
                                        dst.unlink()
                                    src.rename(dst)
                            first = log_dir / f"operation_{date_str}.log.1"
                            if first.exists():
                                first.unlink()
                            log_path.rename(first)
                    except Exception:
                        pass
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"{timestamp} {message}\n")
                except Exception:
                    continue

        thread = threading.Thread(target=writer, name="ESS-AIO-OperationLogWriter", daemon=True)
        thread.start()
        self._operation_log_thread = thread

    def shutdown_async_logging(self, timeout: float = 1.0) -> None:
        try:
            q = getattr(self, "_operation_log_queue", None)
            stop = getattr(self, "_operation_log_stop", None)
            if q is not None and stop is not None:
                try:
                    q.put_nowait(stop)
                except Exception:
                    pass
            thread = getattr(self, "_operation_log_thread", None)
            if thread is not None:
                thread.join(timeout=max(0.0, float(timeout)))
        except Exception:
            pass

    def log(self, message: str) -> None:
        show_in_view = self._should_show_log_message(message)
        if show_in_view:
            self._ensure_log_flush_timer()
            if hasattr(self, "_pending_log_lines"):
                pending = self._pending_log_lines
                pending.append(str(message))
                if len(pending) > 1000:
                    del pending[:len(pending) - 1000]
            else:
                print(message)
        if str(message).startswith("[ERROR]") or str(message).startswith("[CUTOFF]"):
            self.operation_log(str(message))

    def control_log(self, message: str) -> None:
        text = str(message)
        show_in_view = self._should_show_control_log_message(text)
        if show_in_view:
            self._ensure_log_flush_timer()
            if hasattr(self, "_pending_control_log_lines"):
                pending = self._pending_control_log_lines
                pending.append(text)
                if len(pending) > 1000:
                    del pending[:len(pending) - 1000]
            else:
                print(text)
            self.operation_log(text)

    def operation_log(self, message: str) -> None:
        try:
            self._ensure_async_operation_logger()
            log_dir = self.get_profile_path("logs")
            q = getattr(self, "_operation_log_queue", None)
            if q is not None:
                try:
                    q.put_nowait((str(log_dir), str(message)))
                except Exception:
                    # Drop operation log lines under extreme storms rather than
                    # freezing the UI or growing memory unbounded.
                    pass
        except Exception:
            pass

    def handle_load_operation_log(self) -> None:
        default_dir = self.get_profile_path("logs")

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load operation log",
            str(default_dir),
            "Log Files (*.log);;Text Files (*.txt);;All Files (*)",
        )

        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            self.log_text.setPlainText(content)
            self.log(f"[INFO] Loaded operation log: {path}")

        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to load operation log:\n{exc}")

    def handle_clear_log_view(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.clear()

