"""Tools > Clean Up Intermediate Data...: one checkbox per part with its size."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from .datafolder import fmt_size


class CleanupDialog(QDialog):
    def __init__(self, parts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clean Up Intermediate Data")
        self.parts = parts
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Intermediate data that can be recomputed. Finished products "
                             "(point cloud, model, texture, tiles, orthomosaic, DEM) and photos are never deleted."))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Data", "Size", "Note"])
        self.tree.setRootIsDecorated(False)
        for p in parts:
            it = QTreeWidgetItem([(p["chunk"] + ": " if p["chunk"] else "") + p["label"], fmt_size(p["bytes"]),
                                  p["note"]])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if p["checked"] else Qt.Unchecked)
            it.setToolTip(0, "\n".join(p["paths"]))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
        for c in range(3):
            self.tree.resizeColumnToContents(c)
        self.tree.itemChanged.connect(self._update)
        lay.addWidget(self.tree)
        self.total = QLabel()
        lay.addWidget(self.total)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Delete Selected...")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(720, 300)
        self._update()

    def selected(self):
        return [p for k, p in enumerate(self.parts)
                if self.tree.topLevelItem(k).checkState(0) == Qt.Checked]

    def _update(self, *_):
        self.total.setText(f"Selected: {fmt_size(sum(p['bytes'] for p in self.selected()))}")
