"""Shared marker states and actions for Photo, Ortho and Model views."""
from PySide6.QtGui import QColor

COLORS = {"pinned": (40, 220, 60), "placed": (60, 140, 255), "predicted": (190, 190, 190)}

def marker_state(marker):
    if marker.get("disabled", False):
        return "predicted"
    if marker.get("locked", False) or any(len(v) < 3 or v[2] for v in marker.get("projections", {}).values()):
        return "pinned"
    return "placed" if marker.get("projections") else "predicted"

def color(state):
    return QColor(*COLORS.get(state, COLORS["predicted"]))

def marker_menu(menu, label, callback):
    """Actions act on the marker hit by the cursor, never an unrelated active row."""
    for text, action in (("Delete Marker", "delete"), ("Pin Marker", "pin"),
                         ("Ignore Marker", "ignore"), ("Use Marker", "use"),
                         ("Move Marker...", "move")):
        menu.addAction(f"{text}: {label}").triggered.connect(
            lambda _checked=False, action=action: callback(action, label))
