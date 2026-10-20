"""Shared GTK widget helpers used by window UI modules."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from bazzite_portal.artwork.appearance import gaming_mode
from bazzite_portal.input.controller import controller_button_glyph
from bazzite_portal.input.prompts import set_prompt_visible

from bazzite_portal.core.i18n import N_, _, format_message


def label(text="", css=None, selectable=False, *, translate=True):
    # Labels should be readable, but controller focus belongs on controls.
    widget = Gtk.Label(label=format_message(text) if translate else text, xalign=0, wrap=True, selectable=selectable)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, icon=None, *, translate=True):
    if translate:
        text = format_message(text)
    widget = Gtk.Button(label=text)
    widget.controller_label = text
    if icon:
        widget.set_icon_name(icon)
        widget.set_tooltip_text(format_message(text))
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    widget.connect("clicked", lambda *_: callback())
    return widget


def content_page(title=None, description=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    box.set_margin_top(28)
    box.set_margin_bottom(28)
    box.set_margin_start(24)
    box.set_margin_end(24)
    if title is not None:
        box.append(label(title, "title-1"))
    if description is not None:
        box.append(label(description, "dim-label"))
    clamp = Adw.Clamp(maximum_size=760, tightening_threshold=640)
    clamp.set_child(box)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    scroll.set_child(clamp)
    return scroll, box


def group(title, description="", *, translate=True):
    if translate:
        title, description = format_message(title), format_message(description)
    return Adw.PreferencesGroup(title=title, description=description)



def set_window_content(window, content):
    """Scale the entire Gaming Mode window, including input coordinates."""
    if not gaming_mode():
        window.set_content(content)
        return
    from bazzite_portal.ui.scale import scaled_content
    scaled = scaled_content(content, 1.5)
    window.set_content(scaled)
    window.connect("destroy", lambda *_: scaled.detach())
    if not isinstance(window, Adw.ApplicationWindow):
        width, height = window.get_default_size()
        window.set_default_size(round(width * 1.5) if width > 0 else width,
                                round(height * 1.5) if height > 0 else height)


class ControllerLegend(Gtk.Box):
    """Accessible text legend with artwork for supported controller faces."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.set_size_request(-1, 24)
        self._text = ""

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = str(text)
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self.append(Gtk.Label(label=self._text))
        self.update_property([Gtk.AccessibleProperty.LABEL], [self._text])

    def set_controller_content(self, text, prompts, style="auto", *, scrolling=False):
        # Share individual action labels between the visible and accessible legend.
        parts = [("↑↓ " + (_("Scroll") if scrolling else _("Move")))] if text else []
        parts.extend(f"{controller_button_glyph(command, labels)} {format_message(title)}"
                     for command, title, labels in prompts)
        self._text = " · ".join(parts)
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        if text:
            self.append(make_button_prompt("dpad", {}, _("D-pad Scroll") if scrolling else _("D-pad Move"), style))
            move_label = Gtk.Label(label=_("Scroll") if scrolling else _("Move"))
            move_label.add_css_class("dim-label")
            self.append(move_label)
            if prompts:
                self.append(Gtk.Label(label="·"))
        for index, (command, title, labels) in enumerate(prompts):
            title = format_message(title)
            self.append(make_button_prompt(command, labels, _('{title} · controller button').format(title=title), style))
            title_label = Gtk.Label(label=title)
            title_label.add_css_class("dim-label")
            self.append(title_label)
            if index + 1 < len(prompts):
                self.append(Gtk.Label(label="·"))
        self.update_property([Gtk.AccessibleProperty.LABEL], [self._text])


class ControllerPrompt(Gtk.Box):
    """Small face-button artwork while preserving the historic glyph accessor."""

    def __init__(self, command, labels, accessible_name, style="auto"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        accessible_name = format_message(accessible_name)
        self._text = controller_button_glyph(command, labels)
        self.append(make_button_prompt(command, labels, accessible_name, style))
        self.set_size_request(24, 24)
        self.set_tooltip_text(format_message(accessible_name))
        self.update_property([Gtk.AccessibleProperty.LABEL], [accessible_name])

    def get_text(self):
        return self._text


def make_button_prompt(command, labels=None, accessible_name=None, style="auto"):
    labels = labels or {}
    if command == "dpad":
        try:
            from bazzite_portal.input.prompts import button_prompt
            return button_prompt("dpad", "generic", size=24, accessible_name=accessible_name or N_("D-pad Move"),
                                 fallback_label="✥")
        except ImportError:
            return Gtk.Label(label="✥")
    numeric = labels.get(command) if isinstance(labels, dict) else None
    xbox_keys = {"confirm": "a", "back": "b", "focus_search": "x", "context_action": "y"}
    playstation_keys = {"confirm": "cross", "back": "circle", "focus_search": "square", "context_action": "triangle"}
    if style == "xbox":
        family, key = "Xbox Series", xbox_keys.get(command, command)
    elif style == "playstation":
        family, key = "PlayStation", playstation_keys.get(command, command)
    elif numeric in (1, 2, 3, 4):
        family, key = "Xbox Series", {1: "a", 2: "b", 3: "x", 4: "y"}[numeric]
    elif numeric in (5, 6, 7, 8):
        family, key = "PlayStation", {5: "cross", 6: "circle", 7: "square", 8: "triangle"}[numeric]
    elif numeric is None:
        family, key = "Xbox Series", xbox_keys.get(command, command)
    else:
        family, key = "unknown", controller_button_glyph(command, labels)
    fallback = controller_button_glyph(command, labels)
    try:
        from bazzite_portal.input.prompts import button_prompt
        return button_prompt(key, family, size=24, accessible_name=accessible_name,
                             fallback_label=fallback)
    except ImportError:
        prompt = Gtk.Label(label=fallback)
        prompt.set_tooltip_text(format_message(accessible_name or fallback))
        prompt.update_property([Gtk.AccessibleProperty.LABEL], [accessible_name or fallback])
        return prompt


def make_context_prompt(command, labels=None, accessible_name=None, style="auto"):
    return ControllerPrompt(command, labels or {}, accessible_name or command, style)
