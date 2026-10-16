"""Small set of app-owned symbolic SVG icons.

The SVGs are loaded through GTK's icon theme so its symbolic renderer can
recolor them for the active light or dark style.  Path lookup stays usable in
headless tests without importing GTK.
"""

from __future__ import annotations

from pathlib import Path


ICON_NAMES = frozenset(
    {
        "house",
        "scroll-text",
        "circle-check",
        "circle-x",
        "clock",
        "circle-minus",
        "circle-help",
        "shield",
    }
)
_ICON_ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons"
_ICON_THEME_DIR = _ICON_ROOT / "hicolor"
_ICON_DIR = _ICON_THEME_DIR / "scalable" / "actions"
_ICON_PREFIX = "bazzite-portal-"


def icon_path(name: str) -> Path:
    """Return the bundled SVG path for a known icon name."""
    if name not in ICON_NAMES:
        raise ValueError(f"unknown Bazzite Portal icon: {name}")
    path = _ICON_DIR / f"{_ICON_PREFIX}{name}-symbolic.svg"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def icon(name: str, size: int = 20, accessible_label: str | None = None):
    """Create a theme-recolored Gtk.Image for a bundled symbolic icon."""
    if size <= 0:
        raise ValueError("icon size must be positive")

    # Keep GTK optional for callers and tests that only need icon_path().
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    path = icon_path(name)
    display = Gdk.Display.get_default()
    if display is None:
        raise RuntimeError("GTK must be initialized before creating an icon")
    theme = Gtk.IconTheme.get_for_display(display)
    search_paths = theme.get_search_path()
    theme_dir = str(_ICON_ROOT)
    if theme_dir not in search_paths:
        theme.add_search_path(theme_dir)

    image = Gtk.Image.new_from_icon_name(f"{_ICON_PREFIX}{name}-symbolic")
    image.set_pixel_size(size)
    if accessible_label:
        image.update_property(
            [Gtk.AccessibleProperty.LABEL], [accessible_label]
        )
    return image
