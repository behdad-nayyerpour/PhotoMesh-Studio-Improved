"""A message box with named choices (QMessageBox with custom buttons).

Tests replace ask_choice (tests/conftest.py no_dialogs: returns the default)."""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox


def ask_choice(parent, title, text, options, default=None, detail=""):
    """options: [(key, button label)]; a key "cancel" becomes the Escape
    button. Returns the chosen key, or None when the box was closed."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Question)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    buttons = {}
    for key, label in options:
        role = QMessageBox.RejectRole if key == "cancel" else QMessageBox.AcceptRole
        b = box.addButton(label, role)
        buttons[b] = key
        if key == default:
            box.setDefaultButton(b)
        if key == "cancel":
            box.setEscapeButton(b)
    box.exec()
    return buttons.get(box.clickedButton())
