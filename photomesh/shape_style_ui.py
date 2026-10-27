"""Shape style settings window (Tools > Shapes > Shape Settings..., Workspace
shape layer > Layer Style..., double-click on a shape layer).

Per layer, or as an override of the selected shape: line colour / width /
style, point size and symbol, polygon fill colour and opacity, label on/off,
label field, size, colour and background, and on top vs depth-tested in the
3D view. Every change is previewed live in the Model, Ortho and Photo views;
Apply / OK store it in the project (saved with the .pmsx), Cancel restores
the styles the dialog started with, Reset Defaults resets the current target."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QComboBox,
                               QDoubleSpinBox, QSpinBox, QCheckBox, QPushButton, QDialogButtonBox, QLabel,
                               QColorDialog, QSlider, QWidget, QToolButton)

from . import shape_style as SS

SYMBOL_NAMES = {"ring": "Ring (target visible)", "circle": "Filled circle", "square": "Square",
                "diamond": "Diamond", "triangle": "Triangle", "cross": "Cross"}


class ColorButton(QWidget):
    """Colour swatch button (QColorDialog) with an optional 'Auto' state (None)."""
    changed = Signal()

    def __init__(self, parent=None, auto_text=None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(3)
        self.button = QPushButton(self); self.button.setMinimumWidth(110)
        self.button.clicked.connect(self._pick)
        lay.addWidget(self.button)
        self.auto_text = auto_text
        self.auto_button = None
        if auto_text:
            self.auto_button = QToolButton(self); self.auto_button.setText("Auto")
            self.auto_button.setToolTip(f"Use {auto_text}")
            self.auto_button.clicked.connect(lambda: self.set_color(None, emit=True))
            lay.addWidget(self.auto_button)
        self._c = None
        self.fallback = QColor(255, 140, 0)
        self.set_color(None)

    def color(self):
        return None if self._c is None else [self._c.red(), self._c.green(), self._c.blue()]

    def set_color(self, c, emit=False):
        self._c = None if c is None else QColor(int(c[0]), int(c[1]), int(c[2]))
        shown = self._c or self.fallback
        pm = QPixmap(28, 12); pm.fill(shown)
        if self._c is None:
            p = QPainter(pm); p.setPen(QColor(0, 0, 0, 120)); p.drawLine(0, 11, 27, 0); p.end()
        self.button.setIcon(QIcon(pm))
        self.button.setText("Auto" if self._c is None else shown.name())
        self.button.setToolTip(f"Auto: {self.auto_text}" if self._c is None and self.auto_text else
                               "Click to choose a colour")
        if emit:
            self.changed.emit()

    def _pick(self):
        c = QColorDialog.getColor(self._c or self.fallback, self, "Select Colour")
        if c.isValid():
            self.set_color([c.red(), c.green(), c.blue()], emit=True)


class ShapeStyleDialog(QDialog):
    def __init__(self, win, layer, shape=None):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Shape Settings")
        self._start = copy.deepcopy(win.chunk.shapes)        # Cancel restores this
        self._loading = False
        self.shape = shape
        v = QVBoxLayout(self)
        top = QFormLayout()
        self.layer_box = QComboBox(); self.layer_box.addItems(win.shape_layer_names())
        self.target_box = QComboBox(); self.target_box.addItems(["Layer", "Selected shape (override)"])
        self.target_box.setToolTip("Layer: all shapes of the layer. Selected shape: an override for that shape only.")
        top.addRow("Layer:", self.layer_box); top.addRow("Apply to:", self.target_box)
        self.shape_label = QLabel("")
        top.addRow("", self.shape_label)
        v.addLayout(top)

        g = QGroupBox("Lines and outlines"); f = QFormLayout(g)
        self.line_color = ColorButton(self, "the colour from the file, else the layer colour")
        self.line_width = QDoubleSpinBox(); self.line_width.setRange(0.5, 20); self.line_width.setSingleStep(0.5)
        self.line_width.setSuffix(" px")
        self.line_style = QComboBox(); self.line_style.addItems(["Solid", "Dashed"])
        f.addRow("Colour:", self.line_color); f.addRow("Width:", self.line_width); f.addRow("Style:", self.line_style)
        v.addWidget(g)

        g = QGroupBox("Points"); f = QFormLayout(g)
        self.point_size = QDoubleSpinBox(); self.point_size.setRange(2, 60); self.point_size.setSuffix(" px")
        self.point_symbol = QComboBox()
        for k in SS.SYMBOLS:
            self.point_symbol.addItem(SYMBOL_NAMES[k], k)
        f.addRow("Size:", self.point_size); f.addRow("Symbol:", self.point_symbol)
        v.addWidget(g)

        g = QGroupBox("Polygons"); f = QFormLayout(g)
        self.fill_color = ColorButton(self, "the line colour")
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        self.fill_opacity = QSlider(Qt.Horizontal); self.fill_opacity.setRange(0, 100)
        self.fill_value = QLabel("15%"); self.fill_value.setMinimumWidth(34)
        h.addWidget(self.fill_opacity, 1); h.addWidget(self.fill_value)
        f.addRow("Fill colour:", self.fill_color); f.addRow("Fill opacity:", row)
        v.addWidget(g)

        g = QGroupBox("Labels"); f = QFormLayout(g)
        self.label_on = QCheckBox("Show labels")
        self.label_field = QComboBox()
        self.label_size = QDoubleSpinBox(); self.label_size.setRange(5, 48); self.label_size.setSuffix(" pt")
        self.label_color = ColorButton(self, "the line colour")
        self.label_bg = QCheckBox("Background box")
        self.label_bg_color = ColorButton(self)
        f.addRow(self.label_on); f.addRow("Field:", self.label_field); f.addRow("Font size:", self.label_size)
        f.addRow("Colour:", self.label_color)
        bgrow = QWidget(); h = QHBoxLayout(bgrow); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.label_bg); h.addWidget(self.label_bg_color, 1)
        f.addRow("Background:", bgrow)
        v.addWidget(g)

        g = QGroupBox("3D view"); f = QFormLayout(g)
        self.on_top = QComboBox()
        self.on_top.addItems(["Draw on top of the model", "Depth-tested (hidden parts dashed)"])
        self.on_top.setToolTip("On top: never hidden by the mesh / tiled model. Depth-tested: the surface "
                               "hides them; hidden parts are drawn dashed and faded. The transparent view "
                               "always draws shapes on top.")
        f.addRow("Draw:", self.on_top)
        v.addWidget(g)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel |
                                        QDialogButtonBox.RestoreDefaults)
        self.buttons.button(QDialogButtonBox.RestoreDefaults).setText("Reset Defaults")
        self.buttons.accepted.connect(self.ok)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        self.buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self.reset_defaults)
        v.addWidget(self.buttons)

        for w in (self.line_color, self.fill_color, self.label_color, self.label_bg_color):
            w.changed.connect(self._changed)
        for w in (self.line_width, self.point_size, self.label_size):
            w.valueChanged.connect(self._changed)
        for w in (self.line_style, self.point_symbol, self.label_field, self.on_top):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.label_on, self.label_bg):
            w.toggled.connect(self._changed)
        self.fill_opacity.valueChanged.connect(lambda x: (self.fill_value.setText(f"{x}%"), self._changed()))
        self.layer_box.currentIndexChanged.connect(lambda _i: self._load())
        self.target_box.currentIndexChanged.connect(lambda _i: self._load())

        i = self.layer_box.findText(layer)
        self._loading = True
        self.layer_box.setCurrentIndex(max(0, i))
        self.target_box.setCurrentIndex(1 if self._shape_in_layer() else 0)
        self._loading = False
        self._load()

    # ---- target
    def layer(self):
        return self.layer_box.currentText()

    def _layer_index(self):
        sset = self.win._shape_set()
        return next((i for i, L in enumerate(sset["layers"]) if L["name"] == self.layer()), None)

    def _shape_in_layer(self):
        sset = self.win._shape_set()
        return (self.shape is not None and 0 <= self.shape < len(sset["shapes"])
                and sset["shapes"][self.shape]["layer"] == self._layer_index())

    def target_is_shape(self):
        return self.target_box.currentIndex() == 1 and self._shape_in_layer()

    def _load(self):
        """Show the style of the current target."""
        if self._loading:
            return
        from .shapes import shape_name
        sset = self.win._shape_set()
        ok = self._shape_in_layer()
        self.target_box.model().item(1).setEnabled(ok)
        if not ok and self.target_box.currentIndex() == 1:
            self._loading = True; self.target_box.setCurrentIndex(0); self._loading = False
        st = self.win.layer_style(self.layer())
        if self.target_is_shape():
            st.update(SS.shape_override(self.win.chunk.shapes, self.shape))
            self.shape_label.setText(f"Shape: {shape_name(sset['shapes'][self.shape], self.shape)}")
        else:
            n = sum(1 for s in sset["shapes"] if s["layer"] == self._layer_index())
            self.shape_label.setText(f"{n} shapes" + ("" if ok or self.shape is None else
                                                      " (the selected shape is in another layer)"))
        L = sset["layers"][self._layer_index()]
        fields = SS.attribute_fields(sset, self._layer_index())
        self._loading = True
        try:
            self.label_field.clear()
            self.label_field.addItem("Label (from the file)", SS.LABEL_FIELD_FILE)
            self.label_field.addItem("Shape name", SS.LABEL_FIELD_NAME)
            for k in fields:
                self.label_field.addItem(f"[{k}]", k)
            self.set_values(st, L)
        finally:
            self._loading = False

    def set_values(self, st, layer=None):
        if layer is not None:
            lc = layer.get("color") or [255, 140, 0]
            for b in (self.line_color, self.fill_color, self.label_color):
                b.fallback = QColor(*[int(c) for c in lc[:3]])
            if "line_width" not in (SS.clean(((self.win.chunk.shapes or {}).get("styles") or {}).get(layer["name"]))) \
                    and layer.get("width") is not None and st.get("line_width") == SS.DEFAULTS["line_width"]:
                st = dict(st, line_width=float(layer["width"]))
        self.line_color.set_color(st["line_color"])
        self.line_width.setValue(float(st["line_width"]))
        self.line_style.setCurrentIndex(SS.LINE_STYLES.index(st["line_style"]))
        self.point_size.setValue(float(st["point_size"]))
        self.point_symbol.setCurrentIndex(max(0, self.point_symbol.findData(st["point_symbol"])))
        self.fill_color.set_color(st["fill_color"])
        self.fill_opacity.setValue(int(round(100 * float(st["fill_opacity"]))))
        self.fill_value.setText(f"{self.fill_opacity.value()}%")
        self.label_on.setChecked(bool(st["label"]))
        i = self.label_field.findData(st["label_field"])
        if i < 0:                                 # attribute not in this layer: keep it selectable
            self.label_field.addItem(f"[{st['label_field']}]", st["label_field"]); i = self.label_field.count() - 1
        self.label_field.setCurrentIndex(i)
        self.label_size.setValue(float(st["label_size"]))
        self.label_color.set_color(st["label_color"])
        self.label_bg.setChecked(bool(st["label_bg"]))
        self.label_bg_color.set_color(st["label_bg_color"])
        self.on_top.setCurrentIndex(0 if st["on_top"] else 1)

    def values(self):
        return SS.clean({
            "line_color": self.line_color.color(), "line_width": self.line_width.value(),
            "line_style": SS.LINE_STYLES[self.line_style.currentIndex()],
            "point_size": self.point_size.value(), "point_symbol": self.point_symbol.currentData(),
            "fill_color": self.fill_color.color(), "fill_opacity": self.fill_opacity.value() / 100.0,
            "label": self.label_on.isChecked(), "label_field": self.label_field.currentData() or "",
            "label_size": self.label_size.value(), "label_color": self.label_color.color(),
            "label_bg": self.label_bg.isChecked(), "label_bg_color": self.label_bg_color.color() or [20, 20, 24],
            "on_top": self.on_top.currentIndex() == 0})

    # ---- actions
    def _store(self, commit):
        if self.target_is_shape():
            self.win.set_shape_style(self.shape, self.values(), commit=commit)
        else:
            self.win.set_layer_style(self.layer(), self.values(), commit=commit)

    def _changed(self, *_):
        if not self._loading:
            self._store(False)                    # live preview, not yet committed

    def apply(self):
        self._store(True)
        self._start = copy.deepcopy(self.win.chunk.shapes)
        self.win.log(f"Shape style: {'shape ' + str(self.shape) if self.target_is_shape() else 'layer ' + self.layer()}"
                     f" {SS.diff(self.values()) or 'defaults'}")

    def ok(self):
        self.apply(); self.accept()

    def reset_defaults(self):
        st = dict(SS.DEFAULTS)
        if self.target_is_shape():
            st = self.win.layer_style(self.layer())   # override removed -> layer style
        self._loading = True
        try:
            sset = self.win._shape_set()
            self.set_values(st, sset["layers"][self._layer_index()])
        finally:
            self._loading = False
        if self.target_is_shape():
            self.win.set_shape_style(self.shape, None, commit=False)
        else:
            self.win.set_layer_style(self.layer(), None, commit=False)

    def reject(self):
        if self.win.chunk.shapes != self._start:
            self.win.chunk.shapes = copy.deepcopy(self._start)
            self.win._styles_changed(False)
        super().reject()
