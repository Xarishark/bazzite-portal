"""Controller button prompt lookup and GTK presentation helpers.

Asset lookup is kept independent of GTK so callers can resolve prompts and
test the fallbacks in headless environments.
"""

from __future__ import annotations

from pathlib import Path

from bazzite_portal.core.i18n import _


_PROMPT_ASSETS = {
    ("xbox", "a"): "xbox-a.svg",
    ("xbox", "b"): "xbox-b.svg",
    ("xbox", "x"): "xbox-x.svg",
    ("xbox", "y"): "xbox-y.svg",
    ("ps5", "cross"): "ps5-cross.svg",
    ("ps5", "circle"): "ps5-circle.svg",
    ("ps5", "square"): "ps5-square.svg",
    ("ps5", "triangle"): "ps5-triangle.svg",
}
_PAINTABLES = {}


def preload_artwork():
    """Prepare both face packs and D-pad in the startup artwork worker."""
    from bazzite_portal.artwork.home_icon import _rgba_layer
    prepared = {}
    for filename in {*_PROMPT_ASSETS.values(), "dpad.svg"}:
        path = Path(__file__).resolve().parents[1] / "assets" / "prompts" / filename
        if not path.is_file():
            continue
        prepared[str(path)] = _rgba_layer(path.read_text(encoding="utf-8"))
    return prepared


def install_artwork(prepared):
    """Publish immutable prompt textures on the GTK main thread."""
    from gi.repository import Gdk, GLib
    for path, (width, height, data) in prepared.items():
        _PAINTABLES[path] = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                                GLib.Bytes.new(data), width * 4)

_FAMILY_ALIASES = {
    "xbox": "xbox",
    "xbox series": "xbox",
    "xbox-series": "xbox",
    "xbox_series": "xbox",
    "ps5": "ps5",
    "playstation": "ps5",
    "playstation 5": "ps5",
    "playstation-5": "ps5",
    "playstation_5": "ps5",
}

_FACE_ALIASES = {
    "ps5": {
        "a": "cross",
        "b": "circle",
        "x": "square",
        "y": "triangle",
        "✕": "cross",
        "×": "cross",
        "○": "circle",
        "□": "square",
        "△": "triangle",
    },
    "xbox": {
        "🅰": "a",
        "🅱": "b",
    },
}

_FACE_LABELS = {
    "xbox": {"a": "A", "b": "B", "x": "X", "y": "Y"},
    "ps5": {
        "cross": "Cross",
        "circle": "Circle",
        "square": "Square",
        "triangle": "Triangle",
    },
}


def _family_key(family: str) -> str:
    """Return the supported controller family key, or an empty string."""
    return _FAMILY_ALIASES.get(str(family).strip().casefold(), "")


def _button_key(button: str, family_key: str) -> str:
    key = str(button).strip().casefold()
    return _FACE_ALIASES.get(family_key, {}).get(key, key)


def prompt_asset(button: str, family: str) -> Path | None:
    """Return the bundled SVG for a recognized face prompt, if available.

    Unknown buttons and controller families return ``None`` so callers can
    preserve the controller adapter's existing label and mapping.
    """
    if str(button).casefold() == "dpad":
        path = Path(__file__).resolve().parents[1] / "assets" / "prompts" / "dpad.svg"
        return path if path.is_file() else None
    family_key = _family_key(family)
    if not family_key:
        return None
    filename = _PROMPT_ASSETS.get((family_key, _button_key(button, family_key)))
    if not filename:
        return None
    path = Path(__file__).resolve().parents[1] / "assets" / "prompts" / filename
    return path if path.is_file() else None


def prompt_label(button: str, family: str) -> str:
    """Return a readable fallback label, preserving unknown labels verbatim."""
    if str(button).casefold() == "dpad":
        return _("D-pad")
    family_key = _family_key(family)
    if family_key:
        key = _button_key(button, family_key)
        label = _FACE_LABELS.get(family_key, {}).get(key)
        if label:
            return label
    return str(button)


def button_prompt(
    button: str,
    family: str,
    size: int = 24,
    accessible_name: str | None = None,
    fallback_label: str | None = None,
):
    """Create an accessible Gtk.Image or a text fallback for a button prompt.

    GTK is imported lazily so ``prompt_asset`` and ``prompt_label`` remain
    usable by command-line tools and headless tests.
    """
    from gi.repository import Gtk

    name = accessible_name or prompt_label(button, family)
    path = prompt_asset(button, family)
    if path is not None:
        cached = _PAINTABLES.get(str(path))
        widget = Gtk.Image.new_from_paintable(cached) if cached else Gtk.Image.new_from_file(str(path))
        widget.prompt_asset_path = str(path)
        widget.set_pixel_size(max(1, int(size)))
    else:
        text = fallback_label if fallback_label is not None else prompt_label(button, family)
        widget = Gtk.Label(label=str(text))
        widget.set_size_request(max(1, int(size)), max(1, int(size)))
        widget.set_halign(Gtk.Align.CENTER)
        widget.set_valign(Gtk.Align.CENTER)

    widget.set_tooltip_text(name)
    try:
        widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    except (AttributeError, TypeError):
        # Gtk.Label supplies its own accessible text; older GTK bindings may
        # not expose update_property on Gtk.Image.
        pass
    return widget




def set_prompt_visible(widget, visible):
    """Show or fade a prompt without changing its allocated layout space.

    Controller prompts and legends should remain visible to GTK's layout
    system so connecting a controller cannot shift surrounding controls.
    Prompt glyph widgets are decorative and non-interactive; opacity leaves
    the actual controls beside them fully usable.
    """
    visible = bool(visible)
    widget.set_visible(True)
    widget.set_opacity(1.0 if visible else 0.0)
    widget.set_can_target(visible)

    # GTK 4.10+ exposes the hidden accessibility state. Older GTK releases
    # still get stable layout and opacity behavior, but lack this annotation.
    try:
        from gi.repository import Gtk
        hidden_state = getattr(Gtk.AccessibleState, "HIDDEN", None)
        update_state = getattr(widget, "update_state", None)
        if hidden_state is not None and callable(update_state):
            update_state([hidden_state], [not visible])
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
