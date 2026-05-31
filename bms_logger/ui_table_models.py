from __future__ import annotations

from typing import Any, List, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor


class SnapshotTableModel(QAbstractTableModel):
    """Lightweight read-only table model for high-frequency status tables.

    QTableWidget owns one widget item per cell and is expensive when the table is
    rebuilt repeatedly. This model stores plain Python rows and only emits a full
    reset when row/column shape changes; otherwise it emits a single dataChanged
    range for changed values.
    """

    def __init__(self, headers: Sequence[str], parent: Any = None) -> None:
        super().__init__(parent)
        self._headers: List[str] = [str(h) for h in headers]
        self._rows: List[List[str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        try:
            value = self._rows[index.row()][index.column()]
        except Exception:
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            return value
        if role == Qt.ForegroundRole:
            text = str(value).lower()
            if text in {"online", "running", "normal"}:
                return QColor("green")
            if text in {"stale", "scheduled", "timeout", "delay", "limited"}:
                return QColor("orange")
            if text in {"offline", "error", "failed", "active", "charge", "discharge", "cutoff"}:
                return QColor("red")
            if text in {"stopped", "idle"}:
                return QColor("gray")
        if role == Qt.TextAlignmentRole:
            return Qt.AlignCenter
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            try:
                return self._headers[section]
            except Exception:
                return None
        return str(section + 1)

    def set_rows(self, rows: Sequence[Sequence[Any]]) -> None:
        new_rows = [[str(cell) for cell in row] for row in rows]
        if len(new_rows) != len(self._rows) or any(len(r) != len(self._headers) for r in new_rows):
            self.beginResetModel()
            self._rows = new_rows
            self.endResetModel()
            return
        if new_rows == self._rows:
            return

        # Emit the smallest practical changed ranges instead of repainting the
        # whole table. This matters on Windows when 40-60 devices are online.
        old_rows = self._rows
        self._rows = new_rows
        for row_idx, (old, new) in enumerate(zip(old_rows, new_rows)):
            first_col = None
            last_col = None
            for col_idx, (a, b) in enumerate(zip(old, new)):
                if a != b:
                    if first_col is None:
                        first_col = col_idx
                    last_col = col_idx
            if first_col is not None and last_col is not None:
                self.dataChanged.emit(
                    self.index(row_idx, first_col),
                    self.index(row_idx, last_col),
                    [Qt.DisplayRole, Qt.ForegroundRole],
                )

    def clear(self) -> None:
        self.set_rows([])
