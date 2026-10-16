"""GTK window. System operations and the session agent outlive this window."""
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from bazzite_portal import APP_ID
from bazzite_portal import VERSION
from bazzite_portal.integration import tools as portal
from bazzite_portal.integration import community
from bazzite_portal.artwork.appearance import gaming_mode
from bazzite_portal.integration import announcements
from bazzite_portal.integration import favicons
from bazzite_portal.maintenance import deployments
from bazzite_portal.integration import diagnostics
from bazzite_portal.integration import steam as steam_reset
from bazzite_portal.integration import steam as steam_shortcut
from bazzite_portal.ui import system_info as system_presentation
from bazzite_portal.maintenance import reset_layers
from bazzite_portal.maintenance import cancel_pending
from bazzite_portal.artwork.icons import icon as app_icon
from bazzite_portal.input.prompts import set_prompt_visible
from bazzite_portal.maintenance import image_switch
from bazzite_portal.input.controller import (
    SDLGamepad,
    ControllerDriver,
    ControllerPriority,
    controller_button_glyph,
)
from bazzite_portal.core.storage import Store
from bazzite_portal.core.i18n import N_, _, ngettext, format_message
from bazzite_portal.core.errors import error_text, public_error
from bazzite_portal.updates import monitor as system
from bazzite_portal.updates.monitor import system_information
from bazzite_portal.updates.model import Snapshot
from bazzite_portal.updates.release_notes import fetch_release_notes

NOTIFICATION_THEMES_ENABLED = True
log = logging.getLogger(__name__)
PAGES = (N_("Home"), N_("Tools"), N_("Updates"), N_("System"), N_("Management"), N_("Troubleshooting"), N_("Community"), N_("Announcements"), N_("Settings"))
PAGE_LABELS = {
    "Home": _("Bazzite Portal"), "Tools": _("Tools"), "Updates": _("Updates"),
    "System": _("System information"), "Management": _("System management"),
    "Troubleshooting": _("Troubleshooting"), "Community": _("Community"),
    "Announcements": _("Announcements"), "Settings": _("Options"),
}
STATE_LABELS = {
    "running": _("Running"), "success": _("Succeeded"), "failed": _("Failed"),
    "unknown": _("Unknown"), "unavailable": _("Unavailable"),
}
ICONS = ("go-home-symbolic", "applications-utilities-symbolic", "view-refresh-symbolic", "computer-symbolic", "mail-unread-symbolic", "emblem-system-symbolic")


def label(text="", css=None, selectable=False, *, translate=True):
    # Labels should be readable, but controller focus belongs on controls.
    widget = Gtk.Label(label=format_message(text) if translate else text, xalign=0, wrap=True, selectable=selectable)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, icon=None, *, translate=True):
    if translate:
        text = _(text)
    widget = Gtk.Button(label=text)
    widget.controller_label = text
    if icon:
        widget.set_icon_name(icon)
        widget.set_tooltip_text(format_message(text))
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    widget.connect("clicked", lambda *_: callback())
    return widget


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


class UpdateActionButton(Gtk.Button):
    """CTA with a centered label and a contextual prompt inside its trailing edge."""

    def __init__(self, text, callback):
        super().__init__()
        text = format_message(text)
        self.action_label = Gtk.Label(label=text, halign=Gtk.Align.CENTER)
        self.action_content = Gtk.CenterBox()
        self.action_content.set_center_widget(self.action_label)
        self.prompt_slot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.prompt_slot.set_size_request(42, 24)
        self.action_content.set_end_widget(self.prompt_slot)
        self.action_content.set_start_widget(Gtk.Box(width_request=42))
        self.set_child(self.action_content)
        self.controller_label = text
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self.connect("clicked", lambda *_: callback())

    def get_label(self):
        return self.action_label.get_text()

    def set_label(self, text):
        self.action_label.set_text(str(text))
        self.controller_label = str(text)
        self.update_property([Gtk.AccessibleProperty.LABEL], [str(text)])

    def set_prompt(self, prompt):
        prompt.set_size_request(24, 24)
        prompt.set_margin_start(8)
        prompt.set_margin_end(10)
        if (old := self.prompt_slot.get_first_child()) is not None:
            self.prompt_slot.remove(old)
        self.prompt_slot.append(prompt)
        self.action_prompt = prompt


class ModalOptionButton(Gtk.Button):
    """Retain a readable label when an overlay holds the moving prompt."""
    def get_label(self):
        return getattr(self, "controller_label", super().get_label())


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
        parts.extend(f"{controller_button_glyph(command, labels)} {_(title)}"
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
            title = _(title)
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


def install_controller_focus_style():
    display = Gdk.Display.get_default()
    if display is None:
        return None
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"toolbarview undershoot.bottom { background: none; box-shadow: none; } "
        b".bazzite-controller-focus { background-color: alpha(@accent_bg_color, 0.24); "
        b"box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.72); border-radius: 8px; } "
        b".bazzite-controller-immediate { transition: none; } "
        b"button.confirmation-choice:hover { background-color: alpha(@accent_bg_color, 0.20); "
        b"box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.55); } "
        b".bazzite-favorite { color: @warning_color; } "
        b"row.destructive-action .title { color: @error_color; } "
        b"button.bazzite-update-cta { background-color: #8A2BE2; color: #ffffff; } "
        b"button.bazzite-update-cta:hover { background-color: #7424C5; color: #ffffff; } "
        b"box.controller-style-pill { min-height: 36px; padding: 2px 6px; "
        b"background-color: alpha(@theme_fg_color, 0.06); border: 1px solid alpha(@theme_fg_color, 0.16); border-radius: 9px; } "
        b".layer-selection-row { min-height: 36px; padding: 2px 8px; border-radius: 8px; } "
        b".layer-selection-row:hover { background-color: alpha(@theme_fg_color, 0.055); } "
        b".layer-selection-footer { padding-top: 2px; } "
        b"box.community-favicon-tile { background-color: #f5f5f5; "
        b"border: 1px solid #dedede; border-radius: 8px; padding: 0; } "
        b"button.controller-style-arrow { background: transparent; box-shadow: none; padding: 3px; min-width: 28px; min-height: 28px; } "
        b"row.bazzite-update-active-row { background-color: alpha(#8A2BE2, 0.10); } "
        b"row.bazzite-update-step-row .title { font-size: 1.12em; font-weight: 600; } "
        b"row.bazzite-update-step-row .subtitle { font-size: 1.02em; } "
        b".release-table .table-cell, .release-table .table-header { padding: 8px; border: 1px solid alpha(@theme_fg_color, 0.15); } "
        b".release-table .table-header { background: alpha(@theme_fg_color, 0.08); } "
        b".release-table .alternate-row { background: alpha(@theme_fg_color, 0.04); } "
        b"textview.bazzite-update-log, textview.bazzite-update-log text { background-color: transparent; color: @theme_fg_color; }"
    )
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    return provider


def controller_focus_targets(root):
    """Return visible interactive controls in widget order, skipping text labels."""
    target_types = (Gtk.Button, Gtk.Switch, Gtk.SearchEntry, Gtk.Entry, Gtk.DropDown,
                    Gtk.SpinButton, Gtk.CheckButton, Gtk.Scale, Gtk.Expander)
    targets = []

    def visit(widget):
        if (not widget.get_visible() or not widget.get_mapped()
                or (not widget.get_sensitive() and not getattr(widget, "controller_read_only_focus", False))):
            return
        if getattr(widget, "controller_exclude_focus", False):
            return
        if isinstance(widget, Gtk.Stack):
            current = widget.get_visible_child()
            child = widget.get_first_child()
            while child:
                if child is current:
                    visit(child)
                child = child.get_next_sibling()
            return
        if getattr(widget, "controller_focus_row", False):
            widget.set_focusable(True)
            targets.append(widget)
            return
        is_sidebar_row = (isinstance(widget, Gtk.ListBoxRow) and not isinstance(widget, Adw.ActionRow)
                          and not getattr(widget, "controller_skip_self", False))
        # GtkPopoverMenu creates GtkModelButton internally. Its accessible role
        # varies across GTK versions, so recognize the focusable menu item by
        # its GTK type as well as by the semantic role.
        is_menu_item = (
            widget.get_accessible_role() == Gtk.AccessibleRole.MENU_ITEM
            or type(widget).__name__ == "GtkModelButton"
        )
        is_control = isinstance(widget, target_types) or is_sidebar_row or is_menu_item
        if isinstance(widget, Gtk.Button) and not widget.get_label() and not getattr(widget, "controller_label", None):
            is_control = False
        if is_control:
            widget.set_focusable(True)
            targets.append(widget)
        child = widget.get_first_child()
        while child:
            visit(child)
            child = child.get_next_sibling()

    visit(root)
    search = getattr(root, "search", None)
    if getattr(root, "tools_search_active", False) and search in targets:
        targets.remove(search)
        targets.insert(0, search)
    return targets


def portal_status_presentation(token):
    """Match YAFTI's status icon/label mapping for a provided status token."""
    status = token.strip() if isinstance(token, str) else ""
    normalized = status.casefold()
    if normalized in {"install", "active", "enable", "add", "upgraded"}:
        icon = "🟢"
    elif normalized in {"uninstall", "inactive", "disable", "disabled", "remove", "unset", "mismatch"}:
        icon = "🟠"
    elif not normalized or normalized == "unknown":
        return "⚪", _("Unknown")
    else:
        icon = "🔵"
    labels = {"install": _("Installed"), "enable": _("Enabled"), "uninstall": _("Not installed"),
              "disable": _("Disabled"), "remove": _("Removed")}
    return icon, labels.get(normalized, status.capitalize())


def controller_focused_target(root):
    """Map GTK's focused inner text/cell widget to its interactive parent."""
    targets = controller_focus_targets(root)
    logical = getattr(root, "controller_focus_widget", None)
    if (getattr(root, "controller_navigation", False) and logical in targets
            and getattr(logical, "controller_read_only_focus", False)):
        return logical
    focus = root.get_focus()
    while focus:
        if any(focus is target for target in targets) or isinstance(focus, Gtk.SearchEntry):
            return focus
        focus = focus.get_parent()
    return None


def content_page(title, description=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    box.set_margin_top(28)
    box.set_margin_bottom(28)
    box.set_margin_start(24)
    box.set_margin_end(24)
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
        title, description = _(title), _(description)
    return Adw.PreferencesGroup(title=title, description=description)


def system_summary(info):
    status = info.get("deployment_status")
    deployment = None
    if isinstance(status, dict):
        deployments = status.get("deployments")
        if isinstance(deployments, list):
            deployment = next((item for item in deployments if isinstance(item, dict) and item.get("booted") is True), None)
        if deployment is None:
            state = status.get("status")
            booted = state.get("booted") if isinstance(state, dict) else None
            deployment = booted if isinstance(booted, dict) else None

    def value(*values):
        return next((str(item) for item in values if isinstance(item, (str, int, float)) and str(item).strip()), N_("Unavailable"))

    image = N_("Unavailable")
    deployment_version = None
    packages = N_("Unavailable")
    if deployment:
        image_data = deployment.get("image")
        image_data = image_data if isinstance(image_data, dict) else {}
        nested_image = image_data.get("image")
        nested_image = nested_image if isinstance(nested_image, dict) else {}
        image = value(deployment.get("container-image-reference"), deployment.get("origin"),
                      image_data.get("image") if isinstance(image_data.get("image"), str) else image_data.get("imageDigest"),
                      nested_image.get("image"))
        deployment_version = value(deployment.get("version"), image_data.get("version"), nested_image.get("version"))
        package_list = deployment.get("requested-packages", deployment.get("packages"))
        if isinstance(package_list, list):
            names = [str(item) for item in package_list if isinstance(item, str) and item]
            packages = ", ".join(names) if names else N_("None recorded")
    return {
        "Operating system": value(info.get("PRETTY_NAME"), info.get("NAME")),
        "Version": value(deployment_version if deployment else None, info.get("VERSION"), info.get("VERSION_ID")),
        "Image": image,
        "Layered packages": packages,
    }


class Window(Adw.ApplicationWindow):
    def __init__(self, app, config_path, preview=False):
        super().__init__(application=app, title=_("Bazzite Portal"), default_width=1080, default_height=740)
        self.preview = preview
        self.config_path = Path(config_path)
        self.status_received = False
        self.store = Store(base=app.preview_base) if preview else Store()
        self._update_console_open = bool(self.store.preferences().get("show_logs", False))
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="bazzite-portal")
        self.portal_status_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="bazzite-portal-status")
        self.snapshot = Snapshot()
        self.items = []
        self.modal = None
        self.controller = None
        self.controller_navigation = False
        self.controller_connected = False
        self.controller_priority = ControllerPriority()
        self._native_focus_timeout = None
        self._last_pointer_position = {}
        self.controller_focus_widget = None
        self._controller_focus_established = False
        self.controller_focus_style = install_controller_focus_style()
        self.closing = False
        self.log_text = ""
        self._preview_timer = None
        self._preview_step = -1
        self._preview_log_lines = []
        self.client = None
        self._gaming_auth = None
        self._gaming_auth_dialog = None
        self._gaming_auth_entry = None
        self.notices = Adw.ToastOverlay()
        set_window_content(self, self.notices)
        toolbar = Adw.ToolbarView()
        self.toolbar = toolbar
        self.notices.set_child(toolbar)
        header = Adw.HeaderBar()
        self.header = header
        self.header_title = Adw.WindowTitle(title=_("Bazzite Portal"), subtitle="Preview · commands disabled" if preview else "")
        header.set_title_widget(self.header_title)
        self.back_button = Gtk.Button()
        self.back_button.set_icon_name("go-previous-symbolic")
        self.back_button.controller_label = _("Back")
        self.back_button.controller_exclude_focus = True
        self.back_button.set_focusable(True)
        self.back_button.set_size_request(34, 34)
        self.back_button.set_tooltip_text(_("Back"))
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Back")])
        self.back_button.connect("clicked", lambda *_: self._header_back())
        header.pack_start(self.back_button)
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self.stack)
        try:
            self.items = portal.load_config(config_path)
        except Exception as error:
            self.config_error = str(error)
            log.warning("Tools configuration could not be loaded", exc_info=True)
        else:
            self.config_error = ""
        self._build_home()
        self._build_tools()
        self._build_updates()
        self._build_system()
        self._build_troubleshooting()
        GLib.idle_add(self._start_system_prefetch)
        self._build_announcements()
        # Fetch Community artwork in the background as the window starts so
        # opening that page never initiates network work.
        GLib.idle_add(self._ensure_community_favicons)
        self._start_announcements_load()
        self._build_settings()
        self.show_page(N_("Home"))
        self.legend = ControllerLegend()
        self.legend.set_margin_start(18)
        self.legend.set_margin_end(18)
        self.legend.set_margin_top(4)
        self.legend.set_margin_bottom(4)
        set_prompt_visible(self.legend, False)
        toolbar.add_bottom_bar(self.legend)
        self._install_input_mode_controllers(self)
        self.connect("close-request", self._close)
        if not preview:
            from bazzite_portal.services.agent import Client
            self.client = Client(self._snapshot_changed, self.toast)
            self.connect("map", lambda *_: self.client.set_visible(True) if not self.closing else None)
            self.connect("unmap", lambda *_: self.client.set_visible(False) if not self.closing else None)
            if gaming_mode():
                try:
                    from bazzite_portal.services.gaming_auth import GamingAuthServer
                    self._gaming_auth = GamingAuthServer(
                        self.client.connection, self._gaming_auth_challenge,
                        on_ready=self._gaming_auth_ready,
                        on_error=self.toast)
                except Exception:
                    # Keep the ordinary desktop path untouched. Gaming Mode
                    # reports the missing authentication facility to the user.
                    self.toast(N_("Gaming-mode authentication is unavailable."))
        try:
            backend = SDLGamepad()
            self.controller = ControllerDriver(backend, self._controller_context, self._dispatch,
                                               on_connections_changed=self._controller_connections_changed,
                                               on_activity=self._controller_activity)
        except Exception as error:
            self.toast(public_error(error, N_("Controller input is unavailable. Reconnect your controller and try again."), context="Controller initialization"))
        self._render_status()
        self._sync_clear_cache_ui()
        self._refresh_all_prompts()
        set_prompt_visible(self.legend, self._prompts_visible())
        if self.store.errors:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))

    def toast(self, message):
        if not self.closing:
            self.notices.add_toast(Adw.Toast(title=format_message(message)[:500]))

    def _gaming_auth_ready(self, ready):
        if self.client is not None:
            self.client.game_auth_enabled = bool(ready and self._gaming_auth is not None)

    def _gaming_auth_challenge(self, event, text, echo_on):
        if self.closing:
            return
        if event == "completed":
            dialog = self._gaming_auth_dialog
            self._gaming_auth_dialog = None
            if self._gaming_auth_entry is not None:
                self._gaming_auth_entry.set_text("")
            self._gaming_auth_entry = None
            self._gaming_auth_submit = None
            if dialog is not None:
                dialog.close()
            return
        if self._gaming_auth_dialog is None:
            dialog = self._report_modal(_("Administrator authentication"), 480)
            if gaming_mode():
                dialog.fullscreen()
            box = self._report_modal_box(dialog, top_aligned=gaming_mode())
            entry = Gtk.PasswordEntry(show_peek_icon=False)
            entry.set_property("placeholder-text", _("Password"))
            entry.set_property("activates-default", True)
            entry.get_delegate().set_invisible_char("*")
            entry.get_settings().set_property("gtk-entry-password-hint-timeout", 0)
            self._gaming_auth_entry = entry
            box.append(entry)
            box.append(label(_("Enter your administrator password to continue."), "title-3"))
            self._gaming_auth_status = label("", "dim-label")
            self._gaming_auth_status.set_visible(False)
            box.append(self._gaming_auth_status)
            cancel_button = button(_("Cancel"), self._cancel_gaming_auth)
            submit_button = button(_("Authenticate"), self._submit_gaming_auth)
            submit_button.set_sensitive(False)
            submit_button.set_receives_default(True)
            dialog.set_default_widget(submit_button)
            anchor = self._fixed_modal_actions(dialog, box, (
                ("back", cancel_button), ("confirm", submit_button)))
            dialog.connect("close-request", lambda *_: self._gaming_auth_dialog_closed(dialog))
            self._gaming_auth_dialog = dialog
            self._gaming_auth_submit = submit_button
            dialog.connect("map", lambda *_: GLib.idle_add(self._open_auth_keyboard, dialog))
            self._present_report_modal(dialog, entry)
        status = getattr(self, "_gaming_auth_status", None)
        if status is not None:
            if event == "request":
                status.set_text("")
                status.set_visible(False)
                if self._gaming_auth_submit is not None:
                    self._gaming_auth_submit.set_sensitive(True)
            elif event == "info":
                status.set_text(format_message(text or ""))
                status.set_visible(bool(text))
            elif event == "error":
                status.set_text(public_error(text, N_("Authentication failed."), context="Authentication") + "  " + _("Try again or cancel."))
                status.set_visible(True)
        if self._gaming_auth_entry is not None:
            # Keep all entered authentication text masked, including prompts
            # that request echoed input.
            if event == "request":
                self._gaming_auth_entry.grab_focus()

    def _open_auth_keyboard(self, dialog):
        if (not self.closing and dialog is self._gaming_auth_dialog
                and dialog.get_mapped() and self._gaming_auth_entry is not None):
            self._gaming_auth_entry.grab_focus()
            if not self.preview:
                self.work(lambda: subprocess.Popen(
                    ["steam", "-ifrunning", "steam://open/keyboard"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), lambda _: None)
        return GLib.SOURCE_REMOVE

    def _submit_gaming_auth(self, *_):
        entry = self._gaming_auth_entry
        server = self._gaming_auth
        if entry is None or server is None:
            return
        secret = entry.get_text()
        entry.set_text("")
        if not secret:
            entry.grab_focus()
            return
        try:
            server.respond(secret)
        finally:
            secret = ""
            entry.set_text("")

    def _cancel_gaming_auth(self, *_):
        if self._gaming_auth is not None:
            self._gaming_auth.cancel()
        dialog = self._gaming_auth_dialog
        if dialog is not None:
            dialog.close()

    def _gaming_auth_dialog_closed(self, dialog):
        if self._gaming_auth_dialog is dialog:
            self._gaming_auth_dialog = None
            if self._gaming_auth_entry is not None:
                self._gaming_auth_entry.set_text("")
            self._gaming_auth_entry = None
            self._gaming_auth_submit = None
            if self._gaming_auth is not None:
                self._gaming_auth.cancel()
        return self._report_modal_closed(dialog)

    def _install_input_mode_controllers(self, window):
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _controller, x, y, root=window: self._pointer_moved(root, x, y))
        motion.connect("leave", lambda _controller, root=window: self._pointer_left(root))
        window.add_controller(motion)
        window.pointer_motion_controller = motion
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_args: self._native_input_mode())
        window.add_controller(click)
        window.pointer_click_controller = click
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._native_key_pressed)
        window.add_controller(keys)
        window.native_keyboard_controller = keys

    def _native_input_mode(self):
        if not self.controller_priority.request_native_takeover():
            self._schedule_native_focus_clear()
            return
        self.controller_navigation = False
        self._sync_controller_style_mode()
        self._controller_focus_established = False
        self._clear_controller_focus_visuals()

    def _native_key_pressed(self, _controller, keyval, _keycode, state):
        self._native_input_mode()
        if keyval != Gdk.KEY_Escape or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if self.modal:
            self.modal.close()
        elif self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
            self.close_tools_search()
        elif self.stack.get_visible_child_name() != N_("Home"):
            self.show_page(N_("Home"))
        else:
            return False
        return True

    def _header_back(self):
        if self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
            self.close_tools_search()
        else:
            self.show_page(N_("Home"))

    def _clear_controller_focus_visuals(self):
        self.controller_focus_widget = None
        self._controller_focus_established = False
        for root in (self, self.modal):
            if root is None:
                continue
            pending = [root]
            while pending:
                widget = pending.pop()
                if widget.has_css_class("bazzite-controller-focus"):
                    widget.remove_css_class("bazzite-controller-focus")
                widget.remove_css_class("bazzite-controller-immediate")
                if hasattr(widget, "favorite_icon"):
                    self._set_favorite_emphasis(widget, False)
                if hasattr(widget, "controller_selection_prompt"):
                    set_prompt_visible(widget.controller_selection_prompt, False)
                child = widget.get_first_child()
                while child:
                    pending.append(child)
                    child = child.get_next_sibling()
        if hasattr(self, "legend"):
            set_prompt_visible(self.legend, self._prompts_visible())

    def _pointer_moved(self, root, x, y):
        previous = self._last_pointer_position.get(root)
        self._last_pointer_position[root] = (x, y)
        if previous is not None and (x, y) == previous:
            return
        picked = root.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and not hasattr(picked, "favorite_icon"):
            picked = picked.get_parent()
        self._native_input_mode()
        self._set_pointer_hover(picked)

    def _set_pointer_hover(self, row):
        previous = getattr(self, "_pointer_hovered_row", None)
        if previous is not row:
            if previous is not None:
                previous.pointer_hovered = False
                self._set_favorite_emphasis(previous, False)
            self._pointer_hovered_row = row
        if row is not None:
            row.pointer_hovered = True
            self._set_favorite_emphasis(row, False)

    def _pointer_left(self, root):
        self._last_pointer_position.pop(root, None)
        if root is self or root is self.modal:
            self._set_pointer_hover(None)

    def _controller_connections_changed(self, connected):
        self.controller_connected = connected
        self._refresh_all_prompts()
        if not connected:
            self.controller_priority.reset()
            self.controller_navigation = False
            self._sync_controller_style_mode()
            self._controller_focus_established = False
            if self._native_focus_timeout is not None:
                GLib.source_remove(self._native_focus_timeout)
                self._native_focus_timeout = None
            self._clear_controller_focus_visuals()
        self._update_controller_legend()
        for hint in (getattr(self, "options_hint", None), getattr(self, "search_hint", None),
                     getattr(self, "home_updates_hint", None), getattr(self, "update_a_hint", None),
                     getattr(self, "changelog_x_hint", None)):
            if hint is not None:
                set_prompt_visible(hint, self._prompts_visible())
        if hasattr(self, "legend"):
            set_prompt_visible(self.legend, self._prompts_visible())

    def _prompts_visible(self):
        return self.controller_connected or bool(self.store.preferences().get("always_show_controller_prompts", False))

    def _prompt_style(self):
        return self.store.preferences().get("controller_prompt_style", "auto")

    def _refresh_all_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self._refresh_update_prompts(labels)
        self._refresh_context_prompts(labels)
        prompts = getattr(self, "confirmation_prompts", ())
        prompt_labels = getattr(self, "confirmation_prompt_labels", (N_("Yes"), N_("No")))
        for index, (command, accessible) in enumerate(zip(("confirm", "back"), prompt_labels)):
            if index >= len(prompts):
                break
            old = prompts[index]
            prompt = make_button_prompt(command, labels, accessible, self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            prompts[index] = prompt
        if hasattr(self, "legend"):
            self._update_controller_legend(labels)
        self._sync_maintenance_dialog()
        self._refresh_modal_selection_prompts(labels)
        self._refresh_fixed_modal_prompts(labels)

    def _refresh_modal_selection_prompts(self, labels=None):
        if self.modal is None:
            return
        labels = labels or getattr(getattr(self.controller, "backend", None), "button_labels", {})
        prompt_key = (self._prompt_style(), tuple(sorted(labels.items())))
        for target in controller_focus_targets(self.modal):
            overlay = getattr(target, "controller_prompt_overlay", None)
            if overlay is None:
                continue
            if getattr(target, "controller_prompt_key", None) != prompt_key:
                overlay.remove_overlay(target.controller_selection_prompt)
                prompt = make_button_prompt("confirm", labels, N_("Select"), self._prompt_style())
                prompt.set_halign(Gtk.Align.END)
                prompt.set_valign(Gtk.Align.CENTER)
                prompt.set_margin_end(8)
                overlay.add_overlay(prompt)
                overlay.set_measure_overlay(prompt, False)
                target.controller_selection_prompt = prompt
                target.controller_prompt_key = prompt_key
            prompt = target.controller_selection_prompt
            set_prompt_visible(prompt, self.controller_navigation and self._prompts_visible()
                               and target is self.controller_focus_widget)

    def _refresh_update_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, attribute, accessible in (("confirm", "update_a_hint", self.update_button.get_label()),
                                                ("focus_search", "changelog_x_hint", N_("Show changelog"))):
            old = getattr(self, attribute, None)
            if old is None:
                continue
            prompt = make_button_prompt(command, labels, accessible, self._prompt_style())
            prompt.set_size_request(24, 24)
            set_prompt_visible(prompt, self._prompts_visible())
            target = self.update_button if command == "confirm" else self.release_notes_button
            target.set_prompt(prompt)
            setattr(self, attribute, prompt)

    def _refresh_context_prompts(self, labels=None):
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, attribute, accessible in (("context_action", "options_hint", N_("Open Options")),
                                                ("focus_search", "home_updates_hint", N_("Open Updates")),
                                                ("focus_search", "system_refresh_hint", N_("Refresh system information")),
                                                ("focus_search", "management_refresh_hint", N_("Refresh system information")),
                                                ("focus_search", "search_hint", N_("Search for a tool"))):
            old = getattr(self, attribute, None)
            if old is None:
                continue
            prompt = make_context_prompt(command, labels, accessible, self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            setattr(self, attribute, prompt)

    def _controller_activity(self):
        self.controller_priority.mark_activity()
        self.controller_navigation = True
        self._sync_controller_style_mode()
        if self.controller_connected and hasattr(self, "legend"):
            set_prompt_visible(self.legend, True)

    def _schedule_native_focus_clear(self):
        if self._native_focus_timeout is not None:
            return
        delay_ms = max(1, int(self.controller_priority.remaining() * 1000 + 0.999))
        self._native_focus_timeout = GLib.timeout_add(delay_ms, self._deferred_native_focus_clear)

    def _deferred_native_focus_clear(self):
        self._native_focus_timeout = None
        if self.closing:
            return GLib.SOURCE_REMOVE
        if self.controller_priority.native_takeover_due():
            self.controller_navigation = False
            self._sync_controller_style_mode()
            self._clear_controller_focus_visuals()
            return GLib.SOURCE_REMOVE
        if self.controller_priority.native_takeover_pending:
            self._schedule_native_focus_clear()
        return GLib.SOURCE_REMOVE

    def work(self, function, callback, *, executor=None):
        future = (executor or self.executor).submit(function)
        def finished(result):
            if result.cancelled():
                return
            try:
                value, error = result.result(), None
            except Exception as exception:
                log.error("Background task failed", exc_info=True)
                value, error = None, str(exception)
            def deliver():
                if not self.closing:
                    if error:
                        self.toast(public_error(error, N_("The request failed. Open Logs for details."), context="Background task"))
                    else:
                        callback(value)
                return False
            GLib.idle_add(deliver)
        future.add_done_callback(finished)

    def show_page(self, name):
        if name not in PAGES:
            return
        if name == "Tools" and self.config_error:
            return
        if name == "Announcements" and not self.announcements_available:
            return
        previous_page = self.stack.get_visible_child_name()
        if (previous_page == N_("Updates") and name != N_("Updates") and not self.preview
                and self.snapshot.run and not self.snapshot.running
                and self.snapshot.result in {"success", "failed"}):
            self._dismissed_result_run = self.snapshot.run
            self._visible_result_run = None
            self._render_status()
        if previous_page == N_("Home") and name != N_("Home"):
            opener = controller_focused_target(self)
            self._home_return_target = (opener if opener in self.pin_rows else
                                        self.home_category_rows.get(name))
        if name != N_("Updates") and hasattr(self, "update_console_revealer"):
            self._set_update_console(False)
        elif name == N_("Updates") and previous_page != N_("Updates") and hasattr(self, "update_console_revealer"):
            self._update_console_open = bool(self.store.preferences()["show_logs"])
        if name == N_("Updates") and hasattr(self, "updates_content_box"):
            self.updates_content_box.set_valign(
                Gtk.Align.START if self._update_console_open else Gtk.Align.CENTER
            )
        if name == N_("Tools") and self.stack.get_visible_child_name() != N_("Tools"):
            # A visit from another main page starts at the top of the catalog.
            # Do this before revealing Tools so Gtk.Stack cannot animate a
            # stale adjustment into view. Modal returns retain their own context.
            self.tools_search_active = False
            self.search.set_text("")
            self.tools_view.set_visible_child_name("catalog")
            adjustment = self.catalog_scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_lower())
        self.stack.set_visible_child_name(name)
        self.home_options_box.set_visible(name == N_("Home"))
        for prefix in ("system", "management"):
            controls = getattr(self, prefix + "_refresh_header", None)
            if controls is not None:
                controls.set_visible(name == (N_("System") if prefix == "system" else N_("Management")))
        if hasattr(self, "tools_titlebar_search"):
            self.tools_titlebar_search.set_visible(name == N_("Tools"))
        self._render_home_activity()
        self._acknowledge_update_result()
        self._sync_header_back()
        page_title = PAGE_LABELS.get(name, "")
        self.header_title.set_title(format_message(page_title))
        if name in {N_("System"), N_("Management")}:
            self.header_title.set_title(format_message(_("System information") if name == N_("System") else _("System management")))
            if not self.system_has_snapshot:
                # Reveal the page before scheduling even the loading-state changes.
                def load_first_visit():
                    if (not self.closing and not self.system_has_snapshot and
                            self.stack.get_visible_child_name() in {N_("System"), N_("Management")}):
                        self._refresh_system()
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(load_first_visit)
        if hasattr(self, "update_console_revealer"):
            self.update_console_revealer.set_reveal_child(name == N_("Updates") and self._update_console_open)
        self._update_controller_legend()
        def focus_page_start():
            if self.closing or self.modal is not None or self.stack.get_visible_child_name() != name:
                return GLib.SOURCE_REMOVE
            if name == N_("Updates") and self.controller_navigation:
                self._clear_controller_focus_visuals()
                return GLib.SOURCE_REMOVE
            page = self.stack.get_visible_child()
            targets = controller_focus_targets(page) if page else []
            if targets:
                target = getattr(self, "_home_return_target", None) if name == N_("Home") else None
                target = target if target in targets else targets[0]
                if self.controller_navigation:
                    self._set_controller_focus(target)
                    self._reveal_focus(self)
                else:
                    target.grab_focus()
            elif self.back_button.get_sensitive():
                self.back_button.grab_focus()
            self._update_controller_legend()
            return GLib.SOURCE_REMOVE
        if name == N_("Home") and self.controller_navigation:
            focus_page_start()
        else:
            GLib.idle_add(focus_page_start)

    def _build_home(self):
        page, box = content_page(N_("Your Bazzite"), N_("System updates and the tools you use most, in one place."))
        box.remove(box.get_first_child())
        subtitle = box.get_first_child()
        box.remove(subtitle)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.home_options_box = heading
        self.options_button = button(N_("Options"), lambda: self.show_page(N_("Settings")), "preferences-system-symbolic")
        self.options_button.controller_exclude_focus = True
        self.options_button.set_focusable(True)
        self.options_button.set_tooltip_text(_("Options") + " · Y")
        self.options_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Options") + " · Y"])
        self.options_hint = make_context_prompt("context_action", accessible_name=N_("Open Options"))
        set_prompt_visible(self.options_hint, self._prompts_visible())
        heading.append(self.options_hint)
        heading.append(self.options_button)
        self.header.pack_end(heading)
        updates = group("")
        self.home_status = Adw.ActionRow(title=_("Update Bazzite and Apps"), subtitle=_("Update your system and installed apps."))
        self.home_status.set_use_markup(False)
        self.home_status.set_subtitle_lines(2)
        self.home_status.controller_exclude_focus = True
        self.home_status.set_activatable(True)
        self.home_activity_icon = Gtk.Image(pixel_size=32)
        from bazzite_portal.artwork.home_icon import HomeUpdateIcon
        self.home_update_artwork = HomeUpdateIcon(self.home_activity_icon, self.executor)
        self.connect("map", lambda *_: (self._render_home_activity(), self._acknowledge_update_result()))
        self.connect("notify::is-active", lambda *_: self._acknowledge_update_result())
        self.connect("unmap", lambda *_: self._render_home_activity())
        self.stack.connect("notify::visible-child-name", lambda *_: self._render_home_activity())
        self.home_activity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.home_activity.append(self.home_activity_icon)
        self.home_status.add_prefix(self.home_activity)
        self.home_updates_hint = make_context_prompt("focus_search", accessible_name=N_("Open Updates"))
        set_prompt_visible(self.home_updates_hint, self._prompts_visible())
        self.home_status.add_suffix(self.home_updates_hint)
        self.home_status.connect("activated", lambda *_: self.show_page(N_("Updates")))
        updates.add(self.home_status)
        box.append(updates)
        self.home_category_rows = {}
        shortcuts = group(N_("Explore"))
        self.announcements_available = False
        for title, destination, description in (
            (N_("Tools"), N_("Tools"), N_("Browse setup, fixes, and utilities from the system Portal.")),
            (N_("System information"), N_("System"), N_("View deployments, package layers and RPM-OSTree status.")),
            (N_("System management"), N_("Management"), N_("Cancel a pending deployment, remove layered apps or recover your system.")),
            (N_("Troubleshooting"), N_("Troubleshooting"), N_("Report a problem, collect system logs, or reset Steam.")),
            (N_("Community"), N_("Community"), N_("Bazzite documentation, website, and community links.")),
            (N_("Announcements"), N_("Announcements"), N_("View Bazzite release and project announcements.")),
        ):
            row = Adw.ActionRow(title=_(title), subtitle=_(description))
            row.controller_focus_row = True
            row.set_activatable(True)
            missing_or_invalid = destination == "Tools" and bool(self.config_error)
            if destination == "Announcements":
                missing_or_invalid = not announcements.source_path(self.config_path, preview=self.preview).is_file()
            if missing_or_invalid:
                row.set_sensitive(False)
                row.set_activatable(False)
                row.set_subtitle(_("The configuration file for this category is missing or invalid."))
            elif destination == "Announcements":
                row.set_sensitive(False)
                row.set_activatable(False)
                row.set_subtitle(_("Loading…"))
            row.connect("activated", lambda *_args, selected=destination: self.show_page(selected))
            shortcuts.add(row)
            self.home_category_rows[destination] = row
        box.append(shortcuts)
        self.pin_group = group(N_("Pinned tools"))
        box.append(self.pin_group)
        self.pin_rows = []
        self._render_pins()
        self.stack.add_named(page, N_("Home"))
        self._render_home_activity()

    def _build_troubleshooting(self):
        page, box = content_page(N_("Troubleshooting"), N_("Get help, collect diagnostic logs, or repair the Steam client."))
        box.remove(box.get_first_child())
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading.append(label(N_("Troubleshooting"), "title-1"))
        heading.append(Gtk.Box(hexpand=True))
        box.prepend(heading)
        group_box = group("")
        self.report_problem_button = Adw.ActionRow(title=_("Report a problem"), subtitle=_("Collect relevant logs and open a Bazzite GitHub issue."))
        self.report_problem_button.set_activatable(True)
        self.report_problem_button.connect("activated", lambda *_: self._begin_report_flow())
        self.report_problem_button.add_css_class("destructive-action")
        self.report_problem_button.controller_focus_row = True
        group_box.add(self.report_problem_button)
        box.append(group_box)
        group_box = group("")
        self.collect_system_logs_button = Adw.ActionRow(title=_("Collect system logs"), subtitle=_("Save logs and hardware details for troubleshooting on your Desktop."))
        self.collect_system_logs_button.set_activatable(True)
        self.collect_system_logs_button.connect("activated", lambda *_: self._begin_system_logs_collection())
        self.collect_system_logs_button.controller_focus_row = True
        group_box.add(self.collect_system_logs_button)
        box.append(group_box)
        group_box = group("")
        self.reset_steam_button = Adw.ActionRow(title=_("Reset Steam"), subtitle=_("Reset the Steam client without removing your games."))
        self.reset_steam_button.set_activatable(True)
        self.reset_steam_button.connect("activated", lambda *_: self._begin_steam_reset())
        self.reset_steam_button.controller_focus_row = True
        group_box.add(self.reset_steam_button)
        box.append(group_box)
        self.stack.add_named(page, N_("Troubleshooting"))
        self._report_collecting = False
        self._report_generation = 0
        self._troubleshooting_busy = False

    def _begin_report_flow(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        self._report_account_dialog()

    def _set_troubleshooting_busy(self, busy):
        self._troubleshooting_busy = bool(busy)
        for action in (self.report_problem_button, self.collect_system_logs_button, self.reset_steam_button):
            action.set_sensitive(not self._troubleshooting_busy)

    def _report_account_dialog(self):
        dialog = self._report_modal("GitHub account", 510)
        box = self._report_modal_box(dialog)
        box.append(label("Are you signed in to GitHub in your browser?", "title-3"))
        box.append(label("You need a GitHub account to report a problem."))
        no_button = button(N_("No"), lambda: self._close_report_modal(dialog, self._show_account_required))
        yes_button = button(N_("Yes"), lambda: self._close_report_modal(dialog, self._start_report_collection))
        dialog.report_yes_button = yes_button
        dialog.report_no_button = no_button
        anchor = self._fixed_modal_actions(dialog, box, (("back", no_button), ("confirm", yes_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _show_account_required(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        dialog = self._report_modal("GitHub account required", 470)
        box = self._report_modal_box(dialog)
        box.append(label("Sign in to GitHub in your browser. If you don’t have an account, create one first."))
        close_button = button(N_("Close"), dialog.close)
        anchor = self._fixed_modal_actions(dialog, box, (("confirm", close_button),))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _start_report_collection(self):
        if self.closing:
            return
        if self._troubleshooting_busy or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            if self.modal is None:
                GLib.idle_add(self._restore_report_focus)
            return
        self._report_collecting = True
        self._set_troubleshooting_busy(True)
        self._report_generation += 1
        generation = self._report_generation
        self._report_collecting_dialog = None
        if self.preview:
            self._report_collection_finished(generation, SimpleNamespace(
                directory=Path("/tmp/bazzite-report-preview"),
                hardware="Preview hardware summary (collection disabled)",
                files=("bazzite-installation.txt", "current-boot.txt",
                       "previous-boot.txt", "kernel-current-boot.txt", "steam-console.txt"),
                warnings=("Preview only: no files were collected.",),
            ), "", None, None)
            return
        self._show_report_collecting(generation)
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        desktop = Path(desktop) if desktop else Path.home() / N_("Desktop")
        self._report_collecting_dialog = self.modal

        def collect_and_prepare_issue():
            report = diagnostics.collect_report(desktop)
            try:
                issue_url, issue_error = diagnostics.build_issue_url(report), None
            except Exception as exception:
                issue_url, issue_error = "", str(exception)
            return report, issue_url, issue_error

        future = self.executor.submit(collect_and_prepare_issue)

        def finished(result):
            try:
                (report, issue_url, issue_error), error = result.result(), None
            except Exception as exception:
                report, issue_url, issue_error, error = None, "", None, str(exception)
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._report_collection_finished(generation, report, issue_url, issue_error, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _begin_system_logs_collection(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        if self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        self._troubleshooting_busy = True
        self._report_collecting = True
        self._report_generation += 1
        generation = self._report_generation
        self._set_troubleshooting_busy(True)
        self._report_collecting_dialog = None
        if self.preview:
            report = SimpleNamespace(
                directory=Path("/tmp/bazzite-system-logs-preview"),
                hardware="Preview hardware summary (collection disabled)",
                files=(), warnings=("Preview only: no files were collected.",))
            self._system_logs_collection_finished(generation, report, None)
            return
        self._show_report_collecting(generation, N_("Collecting system logs"))
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        desktop = Path(desktop) if desktop else Path.home() / N_("Desktop")
        self._report_collecting_dialog = self.modal
        future = self.executor.submit(diagnostics.collect_system_logs, desktop)

        def finished(result):
            try:
                report, error = result.result(), None
            except Exception as exception:
                report, error = None, str(exception)
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._system_logs_collection_finished(generation, report, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _system_logs_collection_finished(self, generation, report, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        collecting_dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(collecting_dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not collecting_dialog
        if collecting_dialog is not None and self.modal is collecting_dialog:
            collecting_dialog.close()
        self._set_troubleshooting_busy(False)
        if error:
            self.toast(public_error(error, N_('Could not collect system logs. Open Logs for details.'), context='Collect system logs'))
            GLib.idle_add(self._restore_report_focus)
        elif cancelled or superseded:
            self.toast(_('System logs collected and saved to {directory}.').format(directory=report.directory))
            GLib.idle_add(self._restore_report_focus)
        else:
            self._show_system_logs_ready(report)

    def _show_system_logs_ready(self, report):
        dialog = self._report_modal(N_("System logs ready"), 560, 440)
        box = self._report_modal_box(dialog)
        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        review.append(label(N_("System logs are ready."), "title-3"))
        review.append(label(_("Location: {path}").format(path=report.directory)))
        files = "\n".join(f"• {name}" for name in report.files) or _("No files were created.")
        review.append(label(N_("Files collected"), "heading"))
        files_label = label(files, translate=False)
        files_label.set_selectable(True)
        files_label.controller_exclude_focus = True
        review.append(files_label)
        if report.warnings:
            review.append(label(N_("Warnings"), "heading"))
            review.append(label("\n".join(dict.fromkeys(public_error(warning, N_("Some diagnostic information could not be collected. See the collected logs for details."), context="Report collection") for warning in report.warnings)), translate=False))
        scroll = Gtk.ScrolledWindow(min_content_height=120, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(review)
        dialog.report_review_scroll = scroll
        box.append(scroll)
        close_button = button(N_("Close"), dialog.close)
        folder_button = button(N_("Open logs folder"), lambda: self._open_report_folder(report.directory))
        anchor = self._fixed_modal_actions(dialog, box, (("back", close_button), ("confirm", folder_button)))
        dialog.open_folder_button = folder_button
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _begin_steam_reset(self):
        if self.modal is not None or self._troubleshooting_busy:
            return
        if self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._report_restore_target = controller_focused_target(self)
        self._report_restore_with_controller = self.controller_navigation
        dialog = self._report_modal(N_("Reset Steam Installation?"), 560)
        box = self._report_modal_box(dialog)
        box.append(label(N_("Steam will stop and sign you out. This only cleans the steam installation folder.")))
        box.append(label(N_("Your games, Saves, Configuration, Music and Proton files will stay untouched.")))
        cancel_button = button(N_("Cancel"), dialog.close)
        reset_button = button(N_("Reset Steam"), lambda: self._close_report_modal(dialog, self._start_steam_reset))
        reset_button.add_css_class("destructive-action")
        dialog.confirm_yes_button = reset_button
        dialog.confirm_cancel_button = cancel_button
        anchor = self._fixed_modal_actions(dialog, box, (("back", cancel_button), ("confirm", reset_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _start_steam_reset(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Troubleshooting"):
            return
        self._set_troubleshooting_busy(True)
        self._report_collecting = True
        self._report_generation += 1
        generation = self._report_generation
        self._report_collecting_dialog = None
        self._show_report_collecting(generation, N_("Resetting Steam"))
        self._report_collecting_dialog = self.modal
        if self.preview:
            self._steam_reset_finished(generation, None, "Steam reset is disabled in preview mode.")
            return
        future = self.executor.submit(steam_reset.reset_steam)

        def finished(result):
            try:
                outcome, error = result.result(), None
            except Exception as exception:
                outcome, error = None, exception
            def deliver():
                if self.closing or generation != self._report_generation:
                    return GLib.SOURCE_REMOVE
                self._steam_reset_finished(generation, outcome, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)

        future.add_done_callback(finished)

    def _steam_reset_finished(self, generation, outcome, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not dialog
        if dialog is not None and self.modal is dialog:
            dialog.close()
        self._set_troubleshooting_busy(False)
        if error:
            message = public_error(error, N_("Steam reset did not complete. Open Logs for details."), context="Steam reset")
            removed = getattr(error, "removed", ())
            if removed:
                log.warning("Steam reset partially completed; removed: %s", ", ".join(removed))
                message += " " + _("Some Steam data has already been removed.")
            self.toast(message)
        elif cancelled or superseded:
            self.toast(N_("Steam reset finished after its progress window was closed."))
        else:
            self._show_steam_reset_result(outcome)
        GLib.idle_add(self._restore_report_focus)

    def _show_steam_reset_result(self, outcome):
        dialog = self._report_modal(N_("Steam reset complete"), 520)
        box = self._report_modal_box(dialog)
        box.append(label(N_("Steam reset is complete."), "title-3"))
        box.append(label(N_("You can now open Steam. Its client files need to download again, so the first launch may take a while.")))
        close_button = button(N_("Close"), dialog.close)
        close_button.add_css_class("suggested-action")
        box.append(close_button)
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, close_button)

    def _show_report_collecting(self, generation, title=N_("Collecting logs")):
        if self.closing or generation != self._report_generation:
            return
        if self.modal is not None:
            self.modal.close()
        dialog = self._report_modal(title, 430)
        box = self._report_modal_box(dialog)
        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        spinner = Adw.Spinner()
        spinner.set_focusable(False)
        spinner.controller_exclude_focus = True
        progress.append(spinner)
        progress.append(label(N_("Working on this device…")))
        box.append(progress)
        dialog.report_generation = generation
        dialog.report_transient = True
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        close_button = button(N_("Close"), dialog.close)
        anchor = self._fixed_modal_actions(dialog, box, (("back", close_button),))
        self._present_report_modal(dialog, anchor)

    def _report_collection_finished(self, generation, report, issue_url, issue_error, error):
        if self.closing or generation != self._report_generation:
            return
        self._report_collecting = False
        self._set_troubleshooting_busy(False)
        collecting_dialog = getattr(self, "_report_collecting_dialog", None)
        cancelled = bool(getattr(collecting_dialog, "report_cancelled", False))
        superseded = self.modal is not None and self.modal is not collecting_dialog
        if collecting_dialog is not None and self.modal is collecting_dialog:
            collecting_dialog.close()
        if error:
            self.toast(public_error(error, N_('Could not collect diagnostic logs. Open Logs for details.'), context='Collect diagnostic logs'))
            GLib.idle_add(self._restore_report_focus)
            return
        if cancelled or superseded:
            message = _('Logs collected and saved to {directory}.').format(directory=report.directory)
            if issue_error:
                message += " The GitHub issue form could not be prepared."
            self.toast(message)
            GLib.idle_add(self._restore_report_focus)
            return
        self._show_report_ready(report, issue_url, issue_error)

    def _show_report_ready(self, report, issue_url, issue_error=None):
        dialog = self._report_modal(N_("Logs ready"), 800, 720)
        box = self._report_modal_box(dialog)
        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        review.append(label(N_("Logs are ready."), "title-3"))
        review.append(label(_("Location: {path}").format(path=report.directory)))
        hardware = label(str(report.hardware), "monospace", selectable=True, translate=False)
        hardware.set_yalign(0)
        hardware.controller_exclude_focus = True
        hardware.set_max_width_chars(100)
        dialog.report_hardware_selection = hardware
        review.append(label(N_("Hardware summary"), "heading"))
        review.append(hardware)
        files = "\n".join(f"• {name}" for name in report.files) or _("No files were created.")
        review.append(label(N_("Files collected"), "heading"))
        files_label = label(files, translate=False)
        files_label.set_selectable(True)
        files_label.set_wrap(True)
        files_label.controller_exclude_focus = True
        review.append(files_label)
        if report.warnings:
            review.append(label(N_("Warnings"), "heading"))
            review.append(label("\n".join(dict.fromkeys(public_error(warning, N_("Some diagnostic information could not be collected. See the collected logs for details."), context="Report collection") for warning in report.warnings)), translate=False))
        review.append(label(N_("Attach only the logs relevant to your problem. Then fill in the remaining fields and describe the issue.")))
        review_scroll = Gtk.ScrolledWindow(min_content_height=300, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        review_scroll.set_child(review)
        dialog.report_review_scroll = review_scroll
        box.append(review_scroll)
        close_button = button(N_("Close"), dialog.close)
        folder_button = button(N_("Open logs folder"), lambda: self._open_report_folder(report.directory))
        issue_button = button("Open GitHub issue", lambda: self._open_report_issue(issue_url))
        issue_button.add_css_class("suggested-action")
        issue_button.set_sensitive(bool(issue_url))
        if issue_error:
            issue_button.set_tooltip_text(public_error(issue_error, N_("The issue form could not be prepared. Open Logs for details."), context='Prepare issue form'))
        dialog.open_folder_button = folder_button
        dialog.open_issue_button = issue_button
        dialog.hardware_summary = hardware
        dialog.files_label = files_label
        dialog.issue_url = issue_url
        anchor = self._fixed_modal_actions(dialog, box, (
            ("back", close_button), ("context_action", folder_button), ("confirm", issue_button)))
        dialog.connect("close-request", lambda *_: self._report_modal_closed(dialog))
        self._present_report_modal(dialog, anchor)

    def _fixed_modal_actions(self, dialog, box, actions):
        """Keep pad actions fixed while retaining ordinary pointer and Tab access."""
        anchor = Gtk.Box(focusable=True)
        anchor.controller_exclude_focus = True
        anchor.set_size_request(0, 0)
        anchor.set_opacity(0)
        box.append(anchor)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        dialog.fixed_controller_actions = dict(actions)
        dialog.fixed_controller_prompts = []
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        for command, control in actions:
            choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            prompt = make_button_prompt(command, labels, control.get_label(), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            control.controller_exclude_focus = True
            control.add_css_class("confirmation-choice")
            choice.append(prompt)
            choice.append(control)
            row.append(choice)
            dialog.fixed_controller_prompts.append((command, control, prompt))
        box.append(row)
        return anchor

    def _refresh_fixed_modal_prompts(self, labels):
        if self.modal is None:
            return
        picker_legend = getattr(self.modal, "picker_legend", None)
        if picker_legend is not None:
            picker_legend.set_controller_content(_("D-pad Move"), [], self._prompt_style())
            set_prompt_visible(picker_legend, self._prompts_visible())
            wizard = self.modal.image_wizard
            old = wizard.back_prompt
            prompt = make_button_prompt("back", labels, N_("Back"), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            parent.insert_child_after(prompt, old.get_prev_sibling())
            parent.remove(old)
            wizard.back_prompt = prompt
        if getattr(self.modal, "layer_selection", False):
            legend = getattr(self.modal, "layer_legend", None)
            if legend is not None:
                set_prompt_visible(legend, self._prompts_visible())
            for index, (command, title, old) in enumerate(getattr(self.modal, "layer_legend_prompts", ())):
                prompt = make_button_prompt(command, labels, title + " · controller button", self._prompt_style())
                set_prompt_visible(prompt, self._prompts_visible())
                parent = old.get_parent()
                if parent is not None:
                    parent.insert_child_after(prompt, old.get_prev_sibling())
                    parent.remove(old)
                self.modal.layer_legend_prompts[index] = (command, title, prompt)
        prompts = getattr(self.modal, "fixed_controller_prompts", ())
        for index, (command, control, old) in enumerate(prompts):
            prompt = make_button_prompt(command, labels, control.get_label(), self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            parent = old.get_parent()
            if parent is not None:
                parent.insert_child_after(prompt, old.get_prev_sibling())
                parent.remove(old)
            prompts[index] = (command, control, prompt)

    def _open_report_folder(self, directory):
        if self.closing:
            return
        if self.preview:
            self.toast("Folder opening is disabled in preview mode.")
            return
        folder = Path(directory).resolve()
        self._run_xdg_open(str(folder), _("Could not open logs folder"),
                           self._launch_report_folder_worker)

    def _run_xdg_open(self, target, error_prefix, worker=None):
        if self.preview or self.closing:
            return
        future = self.executor.submit(worker or self._launch_xdg_open_worker, target)

        def finished(result):
            try:
                result.result()
            except Exception as error:
                message = public_error(error, _("{action}. Open Logs for details.").format(action=error_prefix), context="Open external location")
                def report_error():
                    if not self.closing:
                        self.toast(message)
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(report_error)

        future.add_done_callback(finished)

    @staticmethod
    def _launch_xdg_open_worker(target):
        process = subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            status = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Some desktop handlers stay running while their window is open.
            return
        if status:
            raise RuntimeError(f"xdg-open exited with status {status}")

    @staticmethod
    def _launch_report_folder_worker(directory):
        return Window._launch_xdg_open_worker(directory)

    def _open_report_issue(self, issue_url):
        if self.closing:
            return
        if self.preview:
            self.toast("Links are disabled in preview mode.")
            return
        if not issue_url:
            self.toast(N_("The GitHub issue form could not be prepared."))
            return
        self._launch_report_uri(issue_url)

    def _launch_report_uri(self, uri):
        if self.closing:
            return
        if self.preview:
            self.toast("Links are disabled in preview mode.")
            return
        self._run_xdg_open(uri, _("Could not open link"))

    def _report_modal(self, title, width, height=-1):
        dialog = Adw.Window(transient_for=self, modal=True, title=_(title), default_width=width,
                            default_height=height)
        dialog.set_application(self.get_application())
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        return dialog

    @staticmethod
    def _report_modal_box(dialog, top_aligned=False):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(22)
        if top_aligned:
            box.set_valign(Gtk.Align.START)
            clamp = Adw.Clamp(maximum_size=560, tightening_threshold=560)
            clamp.set_valign(Gtk.Align.START)
            clamp.set_child(box)
            toolbar.set_content(clamp)
        else:
            toolbar.set_content(box)
        set_window_content(dialog, toolbar)
        return box

    def _present_report_modal(self, dialog, focus_widget):
        dialog.connect("map", lambda *_: GLib.idle_add(
            lambda: self._focus_report_dialog(dialog, focus_widget)))
        dialog.set_focus(focus_widget)
        focus_widget.grab_focus()
        dialog.present()
        self._update_controller_legend()

    def _focus_report_dialog(self, dialog, focus_widget):
        if dialog is self.modal and dialog.get_mapped():
            if self.controller_navigation and not hasattr(dialog, "fixed_controller_actions"):
                self._set_controller_focus(focus_widget)
            else:
                focus_widget.grab_focus()
            hardware = getattr(dialog, "report_hardware_selection", None)
            if hardware is not None:
                hardware.select_region(0, 0)
        return GLib.SOURCE_REMOVE

    def _close_report_modal(self, dialog, callback=None):
        if callback is not None:
            dialog.report_on_close = callback
        dialog.close()

    def _report_modal_closed(self, dialog):
        if self.modal is dialog:
            self.modal = None
        self._last_pointer_position.pop(dialog, None)
        self._update_controller_legend()
        if (getattr(dialog, "report_transient", False) and self._report_collecting
                and getattr(dialog, "report_generation", None) == self._report_generation):
            dialog.report_cancelled = True
        action = getattr(dialog, "report_on_close", None)
        if action is not None and not self.closing:
            GLib.idle_add(lambda: (action(), GLib.SOURCE_REMOVE)[1])
        elif not getattr(dialog, "report_transient", False) and not self.closing:
            GLib.idle_add(self._restore_report_focus)
        return False

    def _restore_report_focus(self):
        target = getattr(self, "_report_restore_target", None)
        if self.closing or self.modal is not None or target is None:
            return GLib.SOURCE_REMOVE
        if target.get_mapped():
            if getattr(self, "_report_restore_with_controller", False) and self.controller_navigation:
                self._set_controller_focus(target)
            else:
                self._native_input_mode()
                target.grab_focus()
        self._report_restore_target = None
        self._update_controller_legend()
        return GLib.SOURCE_REMOVE

    def _render_home_activity(self):
        if not hasattr(self, "home_status"):
            return
        preview_active = bool(self.preview and self._preview_timer is not None)
        running = bool(not self.preview and self.snapshot.running) or preview_active
        if running:
            self.home_status.add_css_class("bazzite-update-active-row")
        else:
            self.home_status.remove_css_class("bazzite-update-active-row")
        self.home_update_artwork.update(
            self.store.preferences()["notification_icon_theme"], running,
            self.get_mapped() and self.stack.get_visible_child_name() == N_("Home"))
        if running:
            if preview_active:
                self.home_status.set_title("Preview update demo · simulated")
                step = max(0, min(self._preview_step, 6))
                self.home_status.set_subtitle(format_message("Simulated update in progress · step {step} of 6.".format(step=step)))
            return
        if self.preview:
            self.home_status.set_title(_("Update Bazzite and Apps"))
            self.home_status.set_subtitle("Try the update preview. System commands are disabled.")
            return

    def _render_pins(self):
        for row in self.pin_rows:
            self.pin_group.remove(row)
        self.pin_rows.clear()
        pins = self.store.pins()
        self.pin_group.set_visible(bool(pins))
        for pin in pins:
            item = portal.resolve(self.items, pin)
            title = item["action"].get("title", N_("Action")) if item else pin.get("title") or pin.get("id", N_("Unavailable tool"))
            subtitle = item["category"] if item else N_("Unavailable or ambiguous · select to remove this pin")
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
            row.pinned_reference = pin
            row.controller_focus_row = True
            row.set_activatable(True)
            if item:
                row.connect("activated", lambda *_args, selected=item: self.open_action(selected))
            else:
                row.connect("activated", lambda *_args, selected=pin: self._remove_unavailable_pin(selected))
            self.pin_group.add(row)
            self.pin_rows.append(row)

    def _remove_unavailable_pin(self, pin):
        self.confirm(
            N_("Remove unavailable pinned tool?"),
            N_("This pin no longer resolves to one unambiguous Portal action. Removing the pin does not run any script."),
            N_("Remove pin"),
            lambda: self.toggle_pin(pin),
        )

    def toggle_pin(self, pin):
        pins = self.store.pins()
        if pin in pins:
            pins.remove(pin)
        elif len(pins) < 100:
            pins.append(pin)
        if not self.store.write("pins", {"items": pins}):
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
        self._render_pins()
        self._sync_tool_favorites()
        self._update_controller_legend()

    def move_pin(self, index, delta):
        pins = self.store.pins()
        if 0 <= index + delta < len(pins):
            pins[index], pins[index + delta] = pins[index + delta], pins[index]
            if not self.store.write("pins", {"items": pins}):
                self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
            self._render_pins()

    def _build_tools(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_vexpand(True)
        page = Adw.Clamp(maximum_size=760, tightening_threshold=640)
        page.set_child(box)
        page.set_vexpand(True)
        self.tools_search_active = False
        self.tools_view = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_button = button(N_("Search"), self.open_tools_search, "system-search-symbolic")
        self.search_button.controller_exclude_focus = True
        self.search_button.set_focusable(True)
        self.search_button.set_tooltip_text(_("Search tools") + " · X")
        self.search_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Search tools") + " · X"])
        self.search_hint = make_context_prompt("focus_search", accessible_name=N_("Search for a tool"))
        set_prompt_visible(self.search_hint, self._prompts_visible())
        search_header.append(self.search_button)
        search_header.append(self.search_hint)
        self.tools_search_header = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.tools_search_header.set_hhomogeneous(False)
        self.tools_search_header.add_named(search_header, "catalog")
        self.tools_titlebar_search = self.tools_search_header
        self.header.pack_start(self.tools_titlebar_search)

        catalog = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        catalog_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.tools_box.set_margin_top(8)
        self.tools_box.set_margin_bottom(8)
        catalog_scroll.set_child(self.tools_box)
        catalog.append(catalog_scroll)
        self.catalog_scroll = catalog_scroll

        search_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.search = Gtk.SearchEntry(placeholder_text=_(N_("Search tools")), hexpand=True)
        self.search.set_size_request(240, -1)
        self.search.update_property([Gtk.AccessibleProperty.LABEL], [N_("Search tools")])
        self.search.connect("search-changed", lambda *_: self._render_tools() if self.tools_search_active else None)
        self.tools_search_header.add_named(self.search, "search")
        search_results_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.search_results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.search_results_box.set_margin_top(8)
        self.search_results_box.set_margin_bottom(8)
        self.search_results_box.set_vexpand(True)
        search_results_scroll.set_child(self.search_results_box)
        search_page.append(search_results_scroll)
        self.search_results_scroll = search_results_scroll
        self.tools_view.add_named(catalog, "catalog")
        self.tools_view.add_named(search_page, "search")
        self.tools_view.connect("notify::visible-child-name", lambda *_:
            self.tools_search_header.set_visible_child_name(self.tools_view.get_visible_child_name()))
        self.tools_view.set_vexpand(True)
        box.append(self.tools_view)
        self._render_tools()
        self.stack.add_named(page, N_("Tools"))

    def open_tools_search(self):
        if self.stack.get_visible_child_name() != N_("Tools"):
            return
        self.tools_search_active = True
        self.tools_view.set_visible_child_name("search")
        self._sync_header_back()
        self.search.set_text("")
        self._render_tools()
        GLib.idle_add(lambda: self.search.grab_focus() and GLib.SOURCE_REMOVE)

    def _open_search_keyboard(self):
        self.work(lambda: subprocess.Popen(["steam", "-ifrunning", "steam://open/keyboard"]), lambda _: None)

    def close_tools_search(self):
        self.tools_search_active = False
        self.tools_view.set_visible_child_name("catalog")
        self._sync_header_back()
        GLib.idle_add(self._focus_tools_start)

    def _sync_header_back(self):
        if not hasattr(self, "back_button"):
            return
        search_back = self.stack.get_visible_child_name() == N_("Tools") and getattr(self, "tools_search_active", False)
        self.back_button.set_tooltip_text(format_message(N_("Back to Tools") if search_back else N_("Back to Home")))
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [N_("Back to Tools") if search_back else N_("Back to Home")])
        self.back_button.set_sensitive(search_back or self.stack.get_visible_child_name() != N_("Home"))

    def _focus_tools_start(self):
        if self.closing or self.stack.get_visible_child_name() != N_("Tools") or self.tools_search_active:
            return GLib.SOURCE_REMOVE
        targets = controller_focus_targets(self.tools_view)
        if targets:
            if self.controller_navigation:
                self._set_controller_focus(targets[0])
            else:
                targets[0].grab_focus()
        return GLib.SOURCE_REMOVE

    def _render_tools(self):
        if not hasattr(self, "tools_box"):
            return
        destination = self.search_results_box if self.tools_search_active else self.tools_box
        destination.portal_status_generation = getattr(destination, "portal_status_generation", 0) + 1
        status_generation = destination.portal_status_generation
        while child := destination.get_first_child():
            destination.remove(child)
        if self.config_error:
            return
        results = portal.search(self.items, self.search.get_text() if self.tools_search_active else "")
        query = self.search.get_text().strip() if self.tools_search_active else ""
        if self.tools_search_active and not query:
            empty = Adw.StatusPage(title=_("Search for a tool"), icon_name="system-search-symbolic")
            empty.set_vexpand(True)
            destination.append(empty)
            return
        if not results:
            destination.append(Adw.StatusPage(title=_("No matching tools"), description=_("Try another search."), icon_name="system-search-symbolic"))
            return
        groups = {}
        pins = self.store.pins()
        for item in results:
            category = item["category"]
            if category not in groups:
                groups[category] = group(category, translate=False)
                destination.append(groups[category])
            action = item["action"]
            row = Adw.ActionRow(title=GLib.markup_escape_text(action.get("title", N_("Action"))), subtitle=GLib.markup_escape_text(action.get("description", "")))
            row.set_title_lines(2)
            row.set_subtitle_lines(3)
            pin = portal.reference(item)
            row.controller_focus_row = True
            row.set_focusable(True)
            row.portal_item = item
            row.portal_reference = pin
            row.set_activatable(True)
            row.connect("activated", lambda *_args, selected=item: self.open_action(selected))
            if action.get("status_script"):
                icon, text = portal_status_presentation("unknown" if self.preview else "")
                status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
                status.set_valign(Gtk.Align.CENTER)
                status_icon = Gtk.Label(label=icon)
                status_text = Gtk.Label(label=text)
                status_icon.set_wrap(False)
                status_text.set_wrap(False)
                status_icon.set_single_line_mode(True)
                status_text.set_single_line_mode(True)
                status.append(status_icon)
                status.append(status_text)
                status.set_tooltip_text("Status unavailable in preview mode." if self.preview else _("Checking current status…"))
                status.update_property([Gtk.AccessibleProperty.LABEL], ["Status unavailable in preview" if self.preview else _("Checking status")])
                row.add_suffix(status)
                row.status_box = status
                row.status_icon = status_icon
                row.status_text = status_text
                row.portal_status_generation = status_generation
                row.portal_status_container = destination
            self._add_favorite_hit_area(row, item, pin in pins)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", lambda _keys, keyval, _keycode, _state, selected=item, target=row:
                         self._action_row_key(selected, target, keyval))
            row.add_controller(keys)
            row.favorite_key_controller = keys
            groups[category].add(row)
            if action.get("status_script") and not self.preview:
                self._request_portal_status(item, row=row, generation=status_generation)

    def _request_portal_status(self, item, *, row=None, generation=None, dialog=None):
        if self.preview or self.closing:
            return
        if row is not None and (getattr(row, "portal_status_pending", False)
                                or hasattr(row, "portal_status_token")):
            if dialog is not None and hasattr(row, "portal_status_token"):
                self._apply_portal_status_to_dialog(dialog, item, row.portal_status_token)
            return
        action = item.get("action", {})
        script = action.get("status_script")
        if not script:
            return
        if row is not None:
            row.portal_status_pending = True

        def status_ready(token):
            if self.closing:
                return
            normalized = token.strip() if isinstance(token, str) else ""
            if row is not None:
                container = row.portal_status_container
                if (generation != getattr(container, "portal_status_generation", 0)
                        or row.portal_status_generation != generation
                        or not row.is_ancestor(container)):
                    return
                row.portal_status_pending = False
                row.portal_status_token = normalized
                icon, status_text = portal_status_presentation(normalized)
                row.status_icon.set_text(icon)
                row.status_text.set_text(status_text)
                unknown = not normalized or normalized.casefold() == "unknown"
                row.status_box.set_tooltip_text(format_message(_("Current status is unknown.") if unknown else _("Current status: {status}").format(status=status_text)))
                row.status_box.update_property([Gtk.AccessibleProperty.LABEL], [_("Status {status}").format(status=status_text)])
            target_dialog = dialog or self.modal
            if (target_dialog is not None and target_dialog is self.modal
                    and getattr(target_dialog, "portal_item", None) is item):
                self._apply_portal_status_to_dialog(target_dialog, item, normalized)

        self.work(lambda: portal.status(script), status_ready, executor=self.portal_status_executor)

    def _add_favorite_hit_area(self, row, item, favorite):
        area = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        area.set_size_request(48, 44)
        # Keep this suffix fixed-width even though its internal spacer expands
        # to right-align the star. Otherwise Gtk.ActionRow may expand the
        # entire suffix and shift preceding status content by row content.
        area.set_hexpand(False)
        area.set_margin_start(12)
        area.set_margin_end(10)
        area.set_valign(Gtk.Align.CENTER)
        area.set_focusable(False)
        area.set_tooltip_text(format_message(_("Remove favorite") if favorite else _("Add favorite")))
        area.update_property([Gtk.AccessibleProperty.LABEL], [_("Remove favorite") if favorite else _("Add favorite")])
        can_favorite = portal.resolve(self.items, portal.reference(item)) is not None
        area.set_sensitive(can_favorite)
        row.favorite_available = can_favorite
        if not can_favorite:
            area.set_tooltip_text(_("This tool has a missing or ambiguous ID"))
            area.update_property([Gtk.AccessibleProperty.LABEL], [_("Favorite unavailable: missing or ambiguous ID")])
        icon = Gtk.Image.new_from_icon_name("starred-symbolic" if favorite else "non-starred-symbolic")
        icon.set_pixel_size(18)
        area.append(Gtk.Box(hexpand=True))
        area.append(icon)
        row.add_suffix(area)
        row.favorite_hit_area = area
        row.favorite_icon = icon
        row.favorite_reference = portal.reference(item)
        self._set_favorite_emphasis(row, False)

        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", lambda click, *_: click.set_state(Gtk.EventSequenceState.CLAIMED))
        gesture.connect("released", lambda *_: self._toggle_favorite(item, row))
        area.add_controller(gesture)
        row.favorite_gesture = gesture

    def _set_favorite_emphasis(self, row, active):
        if not hasattr(row, "favorite_icon"):
            return
        favorite = row.favorite_reference in self.store.pins()
        row.favorite_icon.set_from_icon_name("starred-symbolic" if favorite else "non-starred-symbolic")
        if favorite:
            row.favorite_icon.add_css_class("bazzite-favorite")
        else:
            row.favorite_icon.remove_css_class("bazzite-favorite")
        pointer_reveal = getattr(row, "pointer_hovered", False)
        row.favorite_icon.set_opacity(1.0 if favorite or pointer_reveal else 0.0)
        if row.favorite_available:
            row.favorite_hit_area.set_tooltip_text(format_message(_("Remove favorite") if favorite else _("Add favorite")))
            row.favorite_hit_area.update_property([Gtk.AccessibleProperty.LABEL], [_("Remove favorite") if favorite else _("Add favorite")])

    def _action_row_key(self, item, row, keyval):
        if keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self._native_input_mode()
            self._toggle_favorite(item, row)
            return True
        return False

    def _toggle_favorite(self, item, row=None):
        pin = portal.reference(item)
        if portal.resolve(self.items, pin) is None:
            return
        pins = self.store.pins()
        if pin in pins:
            pins.remove(pin)
        else:
            pins.append(pin)
        if not self.store.write("pins", {"items": pins}):
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
        controller_restore = self.controller_navigation
        self._render_pins()
        self._sync_tool_favorites()
        self._update_controller_legend()
        if row is not None:
            def restore_focus():
                if self.closing or self.stack.get_visible_child_name() != N_("Tools"):
                    return GLib.SOURCE_REMOVE
                target = next((candidate for candidate in controller_focus_targets(self.tools_view)
                               if getattr(candidate, "portal_reference", None) == pin), None)
                if target:
                    if controller_restore:
                        self._set_controller_focus(target)
                    else:
                        target.grab_focus()
                    self._update_controller_legend()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(restore_focus)

    def _sync_tool_favorites(self):
        # Keep both catalog and search rows, including pending status callbacks.
        for root in (self.tools_box, self.search_results_box):
            pending = [root]
            while pending:
                widget = pending.pop()
                if hasattr(widget, "favorite_icon"):
                    self._set_favorite_emphasis(widget, False)
                child = widget.get_first_child()
                while child:
                    pending.append(child)
                    child = child.get_next_sibling()

    def open_action(self, item):
        action = item["action"]
        if action.get("options") or action.get("status_script"):
            self._action_dialog(action, item)
        elif self.store.preferences()["skip_one_shot_confirmation"]:
            self._launch_script(action.get("script"))
        else:
            self._action_dialog(action, item, confirmation=True)

    def _launch_script(self, script, refresh=None):
        if self.preview:
            self.toast("Tool actions are disabled in preview mode.")
            return
        if not script:
            self.toast(N_("No command is configured for this tool."))
            return
        self.work(lambda: portal.terminal(script), lambda _process: refresh() if refresh else None)

    def _apply_portal_status_to_dialog(self, dialog, item, token):
        if dialog is not self.modal or getattr(dialog, "portal_item", None) is not item:
            return
        _icon, status_text = portal_status_presentation(token)
        if getattr(dialog, "portal_status_label", None) is not None:
            unknown = not isinstance(token, str) or token.strip().casefold() in {"", "unknown"}
            dialog.portal_status_label.set_text(_("Current status is unknown.") if unknown
                                                else _("Current status: {status}").format(status=status_text))
        normalized = token.casefold() if isinstance(token, str) else ""
        options = {option.get("id"): option for option in item["action"].get("options") or []}
        for option_id, widget in getattr(dialog, "option_buttons", {}).items():
            widget.remove_css_class("suggested-action")
            if normalized not in ("", "unknown") and option_id == normalized and option_id in options:
                widget.add_css_class("suggested-action")

    def _action_dialog(self, action, item=None, confirmation=False, *, on_confirm=None, packages=None):
        display_action = format_message if on_confirm is not None else lambda text: text
        dialog = Adw.Window(transient_for=self, modal=True,
                            title=display_action((action.get("confirmation_title", _('Run {value1}?').format(value1=action.get('title', 'tool'))) if confirmation else action.get("title", N_("Tool")))),
                            default_width=480)
        dialog.set_application(self.get_application())
        dialog.portal_item = item
        dialog.option_buttons = {}
        self.modal = dialog
        restore = None
        if item is not None and self.stack.get_visible_child_name() == N_("Tools"):
            view_name = "search" if self.tools_search_active else "catalog"
            opener = next((target for target in controller_focus_targets(self.tools_view)
                           if getattr(target, "portal_item", None) is item), None)
            scroll = self.search_results_scroll if self.tools_search_active else self.catalog_scroll
            restore = (opener, view_name, scroll.get_vadjustment().get_value(), self.controller_navigation)
        toolbar = Adw.ToolbarView()
        set_window_content(dialog, toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())
        self._install_input_mode_controllers(dialog)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(24)
        toolbar.set_content(box)
        first_option = None
        if confirmation:
            box.append(label(action.get("title", N_("Tool")), "title-3", translate=on_confirm is not None))
            if on_confirm is None:
                box.append(label("Commands are disabled in preview mode." if self.preview
                                 else N_("This command opens in your desktop terminal."), "heading"))
            if action.get("description"):
                box.append(label(action.get("description", ""), translate=on_confirm is not None))
            if packages is not None:
                box.append(label(ngettext("Package to remove ({count})", "Packages to remove ({count})", len(packages)).format(count=len(packages)), "heading"))
                package_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                for package in packages:
                    package_box.append(label(package, translate=False))
                dialog.package_scroll = Gtk.ScrolledWindow(min_content_height=160, max_content_height=280,
                    propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
                dialog.package_scroll.set_child(package_box)
                box.append(dialog.package_scroll)
            dialog.confirmation_focus_anchor = Gtk.Box()
            dialog.confirmation_focus_anchor.set_focusable(True)
            dialog.confirmation_focus_anchor.controller_exclude_focus = True
            dialog.confirmation_focus_anchor.append(label(action.get("question", _("Do you want to run this tool?")), "heading", translate=on_confirm is not None))
            box.append(dialog.confirmation_focus_anchor)
            choices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                              halign=Gtk.Align.CENTER)
            labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
            self.confirmation_prompts = [
                make_button_prompt(command, labels, N_("Yes") if command == "confirm" else N_("No"), self._prompt_style())
                for command in ("confirm", "back")
            ]
            self.confirmation_prompt_labels = (_("Yes"), _("No"))
            for prompt in self.confirmation_prompts:
                set_prompt_visible(prompt, self._prompts_visible())

            def confirm_yes():
                dialog.close()
                if on_confirm is not None:
                    on_confirm()
                else:
                    self._launch_script(action.get("script"))

            for prompt, text, callback in zip(self.confirmation_prompts, (N_("Yes"), N_("No")),
                                               (confirm_yes, dialog.close)):
                choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                choice.append(prompt)
                control = button(_(text), callback, translate=False)
                control.controller_exclude_focus = True
                control.add_css_class("confirmation-choice")
                choice.append(control)
                choices.append(choice)
                if text == N_("Yes"):
                    first_option = control
                    dialog.confirm_yes_button = control
                else:
                    dialog.confirm_no_button = control
            box.append(choices)
        else:
            box.append(label(action.get("description", ""), translate=on_confirm is not None))
            if self.preview:
                box.append(label("Commands are disabled in preview mode.", "heading"))
            if action.get("status_script"):
                dialog.portal_status_label = label(
                    "Current status is unavailable in preview mode." if self.preview else N_("Checking current status…"),
                    "heading",
                )
                box.append(dialog.portal_status_label)
            def run_option(option):
                dialog.close()
                self._launch_script(option.get("script"))
            for option in action.get("options") or []:
                widget = ModalOptionButton(label=option.get("label", N_("Run")))
                widget.controller_label = option.get("label", N_("Run"))
                widget.update_property([Gtk.AccessibleProperty.LABEL], [widget.controller_label])
                widget.connect("clicked", lambda *_args, selected=option: run_option(selected))
                if option.get("id"):
                    dialog.option_buttons[option["id"]] = widget
                text = widget.get_child()
                text.set_margin_start(34)
                text.set_margin_end(34)
                overlay = Gtk.Overlay()
                widget.set_child(None)
                overlay.set_child(text)
                widget.set_child(overlay)
                prompt = make_button_prompt("confirm", accessible_name=N_("Select"), style=self._prompt_style())
                prompt.set_halign(Gtk.Align.END)
                prompt.set_valign(Gtk.Align.CENTER)
                prompt.set_margin_end(8)
                set_prompt_visible(prompt, False)
                overlay.add_overlay(prompt)
                overlay.set_measure_overlay(prompt, False)
                widget.controller_prompt_overlay = overlay
                widget.controller_selection_prompt = prompt
                box.append(widget)
                if first_option is None:
                    first_option = widget
            if item is not None and action.get("status_script") and not self.preview:
                current_row = next((candidate for candidate in controller_focus_targets(self.tools_view)
                                    if getattr(candidate, "portal_item", None) is item), None)
                if current_row is not None:
                    self._request_portal_status(
                        item, row=current_row,
                        generation=getattr(current_row, "portal_status_generation", None),
                        dialog=dialog,
                    )
                else:
                    self._request_portal_status(item, dialog=dialog)
        # This tool provides status only; it has no action commands.
        def focus_first_option(*_args):
            if dialog is not self.modal or not dialog.get_mapped():
                return GLib.SOURCE_REMOVE
            if confirmation:
                # Keep GTK's native focus away from Yes/No while preserving
                # their normal Tab and pointer access.
                dialog.confirmation_focus_anchor.grab_focus()
                return GLib.SOURCE_REMOVE
            if first_option is None:
                return GLib.SOURCE_REMOVE
            if self.controller_navigation:
                self._set_controller_focus(first_option)
                set_prompt_visible(self.legend, True)
            else:
                first_option.grab_focus()
            self._update_controller_legend()
            return GLib.SOURCE_REMOVE

        dialog.connect("map", lambda *_args: GLib.idle_add(focus_first_option))

        def closed(*_):
            app = self.get_application()
            restore_parent_activation = dialog.is_active() or (app is not None and app.get_active_window() is dialog)
            self._last_pointer_position.pop(dialog, None)
            self.confirmation_prompts = []
            self.modal = None
            if restore is not None:
                def restore_opener():
                    opener, view_name, value, opened_with_controller = restore
                    if self.closing or self.stack.get_visible_child_name() != N_("Tools"):
                        return GLib.SOURCE_REMOVE
                    if restore_parent_activation:
                        self.present()
                    self.tools_search_active = view_name == "search"
                    self.tools_view.set_visible_child_name(view_name)
                    adjustment = self.search_results_scroll.get_vadjustment() if self.tools_search_active else self.catalog_scroll.get_vadjustment()
                    adjustment.set_value(value)
                    if opener is not None and opener.get_mapped():
                        if opened_with_controller and self.controller_navigation:
                            self._set_controller_focus(opener)
                        else:
                            self._native_input_mode()
                            opener.grab_focus()
                    self._update_controller_legend()
                    if not self.controller_navigation:
                        set_prompt_visible(self.legend, self._prompts_visible())
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(restore_opener)
            return False
        dialog.connect("close-request", closed)
        if confirmation:
            dialog.set_focus(dialog.confirmation_focus_anchor)
        dialog.present()

    def _build_updates(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=640)
        clamp.set_child(box)
        page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        page.set_child(clamp)
        self.updates_content_box = box
        box.set_valign(Gtk.Align.START if self.store.preferences()["show_logs"] else Gtk.Align.CENTER)

        controls = Gtk.CenterBox()
        self.update_controls = controls
        action_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.update_action_group = action_group
        self.update_a_hint = make_button_prompt("confirm", accessible_name=N_("Update"), style=self._prompt_style())
        set_prompt_visible(self.update_a_hint, self._prompts_visible())
        self.update_button = UpdateActionButton(N_("Update"), self._primary_update_action)
        self.update_button.set_prompt(self.update_a_hint)
        self.update_button.add_css_class("bazzite-update-cta")
        self.update_button.action_label.add_css_class("title-3")
        action_group.append(self.update_button)
        controls.set_center_widget(action_group)
        self.changelog_x_hint = make_button_prompt("focus_search", accessible_name=N_("Show changelog"), style=self._prompt_style())
        set_prompt_visible(self.changelog_x_hint, self._prompts_visible())
        self.release_notes_button = UpdateActionButton(N_("Changelog"), self._show_release_notes)
        self.release_notes_button.action_label.add_css_class("title-3")
        self.release_notes_button.set_prompt(self.changelog_x_hint)
        self.release_notes_button.set_sensitive(False)
        action_group.append(self.release_notes_button)
        for action in (self.update_button, self.release_notes_button):
            action.set_size_request(220, 56)
        self.update_button.controller_exclude_focus = True
        self.release_notes_button.controller_exclude_focus = True
        box.append(controls)

        self.update_steps_group = Adw.PreferencesGroup()
        self.update_steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.update_steps_group.add(self.update_steps_box)
        self.update_steps_group.set_visible(False)
        box.append(self.update_steps_group)
        if self.preview:
            self._render_preview_steps()

        self.log_buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("bazzite-update-log")
        view.set_margin_top(12)
        view.set_margin_bottom(12)
        view.set_margin_start(12)
        view.set_margin_end(12)
        log_scroll = Gtk.ScrolledWindow(min_content_height=240, max_content_height=320,
                                      propagate_natural_height=True)
        log_scroll.set_child(view)
        self.log_scroll = log_scroll
        self.empty_log_label = label(N_("No log output available yet."), "dim-label")
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        details.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.log_heading = label(N_("Update log"), "heading")
        details.append(self.log_heading)
        details.append(self.empty_log_label)
        details.append(log_scroll)
        details.set_margin_top(12)
        details.set_margin_start(12)
        details.set_margin_end(12)
        self.update_console_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                                   transition_duration=220, reveal_child=False)
        self.update_console_revealer.set_child(details)
        self._update_console_open = bool(self.store.preferences()["show_logs"])
        box.append(self.update_console_revealer)

        self.update_error = label()
        self.update_error.set_selectable(True)
        self.update_error.add_css_class("error")
        self.update_error.set_visible(False)
        box.append(self.update_error)
        self.update_transaction_notice = label(N_("An RPM-OSTree transaction is active. Wait for it to finish before updating or restarting."), "dim-label")
        self.update_transaction_notice.set_visible(False)
        box.append(self.update_transaction_notice)
        self.release_identity = None
        self.stack.add_named(page, N_("Updates"))

    def _step_indicator(self, status, fraction=None):
        normalized = status.casefold().replace("-", "_") if isinstance(status, str) else ""
        if normalized == "finished":
            image = app_icon("circle-check", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Finished · awaiting UUPD result")])
            return image
        if normalized == "success":
            image = app_icon("circle-check", 30)
            image.add_css_class("success")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Completed")])
            return image
        if normalized == "failed":
            image = app_icon("circle-x", 30)
            image.add_css_class("error")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Failed")])
            return image
        if normalized == "pending":
            image = app_icon("clock", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Waiting to start")])
            return image
        if normalized == "skipped":
            image = app_icon("circle-minus", 30)
            image.add_css_class("dim-label")
            image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Skipped")])
            return image
        if normalized in {"active", "running", "in_progress", "in-progress"}:
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and 0 <= fraction <= 1:
                ring = Gtk.DrawingArea()
                ring.set_content_width(30)
                ring.set_content_height(30)
                percent = round(fraction * 100)
                ring.update_property([Gtk.AccessibleProperty.LABEL], ['Preview demo, {percent}% complete'.format(percent=percent) if self.preview else _('{percent}% complete').format(percent=percent)])
                def draw(_area, cr, width, height):
                    cx, cy, radius = width / 2, height / 2, min(width, height) / 2 - 2
                    cr.set_line_width(2.4)
                    cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
                    cr.arc(cx, cy, radius, 0, 6.283185307)
                    cr.stroke()
                    cr.set_source_rgba(0.541, 0.169, 0.886, 1.0)
                    cr.arc(cx, cy, radius, -1.570796327, -1.570796327 + 6.283185307 * fraction)
                    cr.stroke()
                ring.set_draw_func(draw)
                return ring
            spinner = Adw.Spinner()
            spinner.set_size_request(30, 30)
            spinner.update_property([Gtk.AccessibleProperty.LABEL], [N_("In progress")])
            return spinner
        image = app_icon("circle-help", 30)
        image.add_css_class("dim-label")
        image.update_property([Gtk.AccessibleProperty.LABEL], [N_("Status unavailable")])
        return image

    def _render_update_steps(self, modules):
        rows = getattr(self, "_update_step_rows", {})
        retained = {}
        if not isinstance(modules, list) or not modules:
            for row in rows.values():
                self.update_steps_box.remove(row)
            self._update_step_rows = {}
            self.update_steps_group.set_visible(False)
            return
        self.update_steps_group.set_visible(True)
        reading_progress = self.snapshot.running and not any(
            isinstance(module, dict) and module.get("status") in {"running", "finished", "success", "failed"}
            for module in modules)
        previous = None
        for module in modules:
            if not isinstance(module, dict):
                continue
            name = str(module.get("name") or N_("Update step"))
            identity = str(module.get("id") or name)
            if identity == "system" or name == N_("System"):
                name = N_("Bazzite System")
            status = str(module.get("status") or "unknown")
            status_text = {"pending": N_("Waiting to update"), "skipped": N_("Skipped"), "unknown": N_("Status unavailable")}.get(status, status.replace("_", " ").capitalize())
            detail = str(module.get("error") or module.get("detail") or "").strip()
            if status == "failed" or module.get("error"):
                detail = public_error(detail, N_("Update failed. Open the update log for details."), context="Update step: " + identity)
            up_to_date = identity == "system" and status == "skipped" and detail == N_("No system update is available.")
            if up_to_date:
                status = "success"
            if status == "finished":
                detail = N_("Finished · awaiting update result")
            elif status == "success" and identity in {"brew", "flatpak", "system"} and not up_to_date:
                detail = N_("Updated")
            if status == "success" and identity == "system" and not up_to_date and getattr(self.snapshot, "deployment", ""):
                detail = N_("New version ready on next restart")
            elif status == "pending":
                detail = (detail if self.preview and detail else
                          N_("Reading update progress…") if reading_progress else status_text)
            elif not detail:
                detail = status_text
            row = rows.get(identity)
            if row is None:
                row = Adw.ActionRow()
                row.set_use_markup(False)
                row.set_subtitle_lines(1)
                row.add_css_class("bazzite-update-step-row")
                row.set_focusable(False)
                row.controller_exclude_focus = True
                self.update_steps_box.append(row)
            row.set_title(format_message(name))
            row.set_subtitle(format_message(detail))
            indicator_state = (status, module.get("fraction"))
            if getattr(row, "indicator_state", None) != indicator_state:
                if hasattr(row, "step_indicator"):
                    row.remove(row.step_indicator)
                row.step_indicator = self._step_indicator(*indicator_state)
                row.indicator_state = indicator_state
                row.add_prefix(row.step_indicator)
            if module.get("logs_missing") or module.get("logs_available") is False:
                row.set_subtitle(" · ".join(filter(None, [row.get_subtitle(), _("Logs unavailable")])))
            elif module.get("logs_available") is True and not module.get("logs"):
                row.set_subtitle(" · ".join(filter(None, [row.get_subtitle(), _("No module log output")])))
            self.update_steps_box.reorder_child_after(row, previous)
            previous = row
            retained[identity] = row
        for identity, row in rows.items():
            if identity not in retained:
                self.update_steps_box.remove(row)
        self._update_step_rows = retained

    def _deployment_transaction_active(self):
        management = self.snapshot.management
        if isinstance(management, dict) and management:
            return bool((management.get("summary") or {}).get("transaction"))
        return bool((self.image_deployment_status or {}).get("transaction"))

    def _management_blocker_reason(self, excluding=None):
        """Return the shared explanation when another system action is active."""
        if excluding and self._maintenance_row_should_open(excluding):
            return ""
        if self._deployment_transaction_active():
            return N_("A system operation is running. Wait for it to finish.")
        state = self.snapshot
        active = set()
        if state.running:
            active.add("update")
        checks = {
            "rollback": (getattr(self, "rollback_request_active", False)
                         or getattr(state, "rollback_state", "") == "running"),
            "cancel": (getattr(self, "cancel_request_active", False)
                       or getattr(self, "cancel_waiting_snapshot", False)
                       or getattr(state, "cancel_state", "") == "running"),
            "layer": (getattr(self, "layers_request_active", False)
                      or getattr(state, "layer_state", "") == "running"),
            "cache": (getattr(self, "cache_request_active", False)
                      or getattr(state, "cache_state", "") == "running"),
            "channel": (getattr(self, "channel_request_active", False)
                        or getattr(state, "channel_state", "") == "running"
                        or isinstance(self.channel_status, dict)
                        and self.channel_status.get("state") == "running"),
            "image": (getattr(self, "image_request_active", False)
                      or getattr(state, "image_state", "") == "running"
                      or isinstance(self.image_status, dict)
                      and self.image_status.get("state") == "running"),
            "delete": (getattr(self, "delete_request_active", False)
                       or getattr(state, "delete_state", "") == "running"
                       or isinstance(self.delete_status, dict)
                       and self.delete_status.get("state") == "running"),
        }
        active.update(kind for kind, running in checks.items() if running)
        active.update(kind for kind in getattr(self, "maintenance_pending_units", {})
                      if kind in checks)
        active.discard(excluding)
        if active:
            return N_("A system operation is running. Wait for it to finish.")
        return ""

    def _primary_update_action(self):
        if self.preview:
            self._start_preview_update()
            return
        if not self.status_received:
            return
        if self._deployment_transaction_active():
            return
        if (getattr(self, "_update_request_pending", False) or self.snapshot.running or self.snapshot.cancel_state == "running"
                or self.snapshot.layer_state == "running"):
            return
        if not self.snapshot.available:
            return
        if self.snapshot.deployment:
            if not self.snapshot.deployment_known:
                return
            self.confirm(N_("Reboot Bazzite?"), N_("All update steps have finished. Reboot now to start the prepared Bazzite version. Save your work before continuing."), N_("Reboot"), lambda: self._operation(N_("Restart")))
            return
        self._awaiting_update_run = self.snapshot.run
        self._update_request_pending = True
        self._set_log_text("")
        self._render_status()
        self._operation("StartUpdate")

    def _show_release_notes(self):
        if self.modal or self.closing or not self.release_identity:
            return
        identity = dict(self.release_identity)
        dialog = Adw.Window(transient_for=self, modal=True, default_width=900, default_height=680,
                            title=identity["role"] + " changelog · " + identity["version"])
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        set_window_content(dialog, toolbar)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_margin_top(16)
        scroll.set_margin_bottom(16)
        scroll.set_margin_start(20)
        scroll.set_margin_end(20)
        scroll.set_child(label("Loading the matching GitHub release notes…"))
        toolbar.set_content(scroll)
        dialog.maintenance_scroll = scroll
        dialog.maintenance_legend = ControllerLegend()
        dialog.maintenance_legend.set_margin_start(20)
        dialog.maintenance_legend.set_margin_end(20)
        dialog.maintenance_legend.set_margin_top(8)
        dialog.maintenance_legend.set_margin_bottom(16)
        toolbar.add_bottom_bar(dialog.maintenance_legend)
        self.modal = dialog
        self._sync_maintenance_dialog()
        self._install_input_mode_controllers(dialog)
        def closed(*_):
            if self.modal is dialog:
                self.modal = None
            self._last_pointer_position.pop(dialog, None)
            def restore():
                if not self.closing:
                    self.present()
                    if self.controller_navigation:
                        self._clear_controller_focus_visuals()
                    else:
                        self.release_notes_button.grab_focus()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(restore)
            return False
        dialog.connect("close-request", closed)
        if gaming_mode():
            dialog.fullscreen()
        dialog.present()
        def loaded(result):
            if self.modal is not dialog:
                return
            if result.get("state") == "available":
                body = result.get("body") or N_("This release has no written notes.")
                from bazzite_portal.ui.release_notes_view import build_release_notes
                scroll.set_child(build_release_notes(body))
            else:
                scroll.set_child(label(error_text(result.get("error"), N_("Could not load the changelog. Try again later."), context="Changelog") or N_("No matching release notes were found.")))
        self.work(lambda: fetch_release_notes(identity["version"], identity["channel"]), loaded)

    def _set_release_identity(self, summary):
        entries = summary.get("entries", []) if isinstance(summary, dict) else []
        selected = next((entry for entry in entries if entry.get("role") == N_("Next restart")), None)
        selected = selected or next((entry for entry in entries if entry.get("role") == N_("Running now")), None)
        self.release_identity = None
        if selected:
            channel = system_presentation._track(selected.get("image")).lower()
            version = selected.get("version", N_("Unknown"))
            if channel in ("stable", "testing") and version != N_("Unknown"):
                self.release_identity = {"version": version, "channel": channel,
                                         "role": N_("Pending update") if selected["role"] == N_("Next restart") else N_("Installed version")}
        self.release_notes_button.set_sensitive(self.release_identity is not None)
        if self.release_identity:
            self.release_notes_button.set_tooltip_text(format_message(self.release_identity["role"] + " changelog · " + self.release_identity["version"]))
        else:
            self.release_notes_button.set_tooltip_text(_("No verified release version and channel are available."))

    def _start_preview_update(self):
        if not self.preview or self.closing or self._preview_timer is not None:
            return
        self._preview_step = 0
        self._preview_log_lines = ["[SIMULATED] Preview only. No system commands are run."]
        self.log_heading.set_text("Preview log · simulated only")
        self._set_log_text("\n".join(self._preview_log_lines), follow_tail=True)
        self._render_preview_steps()
        self.update_button.set_sensitive(False)
        self._preview_timer = GLib.timeout_add_seconds(1, self._advance_preview_update)
        self._render_home_activity()

    def _render_preview_steps(self):
        self.update_steps_group.set_title("Preview steps · simulated")
        step = self._preview_step
        prepare_status = "success" if step > 0 else "running" if step == 0 else "pending"
        flatpak_status = "success" if step >= 5 else "running" if 1 <= step <= 4 else "pending"
        finish_status = "success" if step >= 6 else "running" if step == 5 else "pending"
        rows = [
            {"name": "Preparing preview", "status": prepare_status,
             "detail": "Simulated setup" if step > 0 else "Preview demo in progress" if step == 0 else "Waiting · simulated"},
            {"name": "Flatpak apps · simulated", "status": flatpak_status,
             "detail": ('Preview demo · {step} of 5 apps'.format(step=step) if 1 <= step <= 4 else "Simulated app updates"),
             "fraction": step / 5 if 1 <= step <= 4 else None},
            {"name": "Finishing preview", "status": finish_status,
             "detail": "Simulated completion" if step >= 6 else "Waiting"},
        ]
        self._render_update_steps(rows)

    def _advance_preview_update(self):
        if self.closing:
            self._preview_timer = None
            return GLib.SOURCE_REMOVE
        self._preview_step += 1
        self._render_preview_steps()
        step_name = ("Preparing preview", "Checking simulated apps", "Downloading simulated apps",
                     "Installing simulated apps", "Finishing simulated apps", "Completing preview")[min(self._preview_step - 1, 5)]
        self._preview_log_lines.append(f"[SIMULATED] {step_name} · step {self._preview_step} of 6")
        self._set_log_text("\n".join(self._preview_log_lines), follow_tail=True)
        if self._preview_step >= 6:
            self._preview_timer = None
            self.update_button.set_sensitive(True)
            self._render_home_activity()
            return GLib.SOURCE_REMOVE
        self._render_home_activity()
        return GLib.SOURCE_CONTINUE

    def _set_update_console(self, open_console):
        open_console = bool(open_console)
        if hasattr(self, "updates_content_box"):
            self.updates_content_box.set_valign(Gtk.Align.START if open_console else Gtk.Align.CENTER)
        was_open = self._update_console_open
        if open_console and not was_open:
            self._update_log_previous_focus = self.get_focus()
        self._update_console_open = open_console
        visible = self.stack.get_visible_child_name() == N_("Updates") and self._update_console_open
        self.update_console_revealer.set_reveal_child(visible)
        if was_open and not open_console:
            restore = (self.controller_focus_widget if self.controller_navigation
                       else getattr(self, "_update_log_previous_focus", None))
            if restore is not None and restore.get_visible() and restore.get_sensitive():
                restore.grab_focus()

    def _snapshot_changed(self, data):
        previous_run = self.snapshot.run
        previously_received = self.status_received
        previous_running = self.snapshot.running
        previous_deployment = self.snapshot.deployment
        previous_cancel_state = getattr(self.snapshot, "cancel_state", "unavailable")
        previous_layer_state = getattr(self.snapshot, "layer_state", "unavailable")
        allowed = Snapshot.__dataclass_fields__
        self.snapshot = Snapshot(**{key: value for key, value in data.items() if key in allowed})
        viewed_records = data.get("maintenance_viewed", ())
        if not isinstance(viewed_records, (list, tuple)):
            viewed_records = ()
        service_acknowledged = set()
        for record in viewed_records:
            if not isinstance(record, (list, tuple)) or len(record) != 4:
                continue
            kind, unit, invocation, generation = record
            if (isinstance(kind, str)
                    and kind in {"rollback", "cancel", "cache", "layer", "delete", "channel", "image"}
                    and isinstance(unit, str) and 0 < len(unit) <= 255
                    and isinstance(invocation, str) and len(invocation) <= 32
                    and type(generation) is int and 0 <= generation <= 2**63 - 1):
                service_acknowledged.add((kind, unit, invocation, generation))
        pending_acknowledgements = getattr(self, "maintenance_acknowledgement_pending", set())
        pending_acknowledgements.difference_update(service_acknowledged)
        pending_acknowledgements = {
            key for key in pending_acknowledgements
            if self._maintenance_key(key[0]) == key[1:]
        }
        self.maintenance_acknowledgement_pending = pending_acknowledgements
        self.maintenance_acknowledged = service_acknowledged | pending_acknowledgements
        self.status_received = True
        shared_management = bool(self.snapshot.management)
        if shared_management:
            self._apply_management_snapshot(self.snapshot.management)
        if self.snapshot.running or self.snapshot.run != getattr(self, "_awaiting_update_run", None):
            self._awaiting_update_run = None
            self._update_request_pending = False
        if self.snapshot.run and (self.snapshot.running or
                (previously_received and self.snapshot.run != previous_run)):
            self._observed_update_run = self.snapshot.run
        if self.cancel_waiting_snapshot and (
                self.snapshot.cancel_state not in {"idle", "unavailable"}
                or not self.snapshot.deployment_known
                or not self.snapshot.deployment
                or (self.cancel_target and self.cancel_target.get("identity") != self.snapshot.deployment)):
            self.cancel_waiting_snapshot = False
            self.cancel_feedback = None
        if not shared_management and (previous_deployment != self.snapshot.deployment or
                (not previous_running and self.snapshot.running)):
            self.deployment_summary_fresh = False
        cancel_finished = (previous_cancel_state == "running"
                           and self.snapshot.cancel_state in {"success", "failed", "idle", "unknown"})
        if cancel_finished and not shared_management:
            self.cancel_target_fresh = False
        self._render_status()
        self._acknowledge_update_result()
        if (not shared_management and not self.preview and not self.system_loading and
                (previous_deployment != self.snapshot.deployment or
                 (previous_running and not self.snapshot.running))):
            # Refresh the known changelog version after staging/completion.
            # This reads deployment metadata in a worker, without a network call.
            self._refresh_system()
        if hasattr(self, "layers_row"):
            self._sync_layers_ui()
        if hasattr(self, "clear_cache_row"):
            self._sync_clear_cache_ui()
        self._sync_maintenance_dialog()
        if cancel_finished and not shared_management:
            self._refresh_system_after_cancel()
        if previous_layer_state == "running" and getattr(self.snapshot, "layer_state", "unknown") != "running":
            if not shared_management:
                self.cancel_target_fresh = False
            self._sync_layers_ui()
            if not shared_management:
                self._refresh_system_after_cancel()
        self._maybe_open_maintenance_details()

    def _set_log_text(self, text, follow_tail=False):
        text = str(text)[-12000:]
        has_logs = bool(text)
        self.log_scroll.set_visible(has_logs)
        self.empty_log_label.set_visible(not has_logs)
        if text == self.log_text:
            return
        adjustment = self.log_scroll.get_vadjustment()
        was_at_bottom = adjustment.get_upper() - (adjustment.get_value() + adjustment.get_page_size()) <= 8
        self.log_buffer.set_text(text)
        self.log_text = text
        if follow_tail and was_at_bottom:
            def scroll_tail():
                adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
                return GLib.SOURCE_REMOVE
            GLib.idle_add(scroll_tail)

    def _acknowledge_update_result(self):
        state = self.snapshot
        if (not self.preview and self.client is not None and self.get_mapped()
                and self.stack.get_visible_child_name() == N_("Updates")
                and not state.running and state.result in {"failed", "success"} and state.run):
            if not self.is_active():
                return
            if state.result_viewed_run == state.run or getattr(self, "_dismissed_result_run", None) == state.run:
                return
            self._visible_result_run = state.run
            self.client.updates_viewed(state.run)

    def _render_status(self):
        state = self.snapshot
        historical_run = bool(state.run and not state.running
                              and state.run != getattr(self, "_observed_update_run", ""))
        dismissed_result = (not state.running and state.result in {"success", "failed"}
                             and (state.run == getattr(self, "_dismissed_result_run", None)
                                  or (state.run and state.result_viewed_run == state.run
                                      and state.run != getattr(self, "_visible_result_run", None))))
        awaiting_run = getattr(self, "_awaiting_update_run", None) == state.run
        if self.preview:
            self._render_preview_steps()
        else:
            self.update_steps_group.set_title(format_message(""))
            previous_update = bool(state.module_plan and awaiting_run)
            modules = state.module_plan if previous_update else state.modules
            if dismissed_result:
                modules = state.module_plan or [
                    {"id": row.get("id"), "name": row.get("name"), "status": "pending"}
                    for row in state.modules if isinstance(row, dict)
                ]
            if not self.store.preferences()["show_disabled_update_steps"]:
                disabled = {row["id"] for row in state.module_plan if row.get("status") == "skipped"}
                modules = [row for row in modules if row.get("id") not in disabled]
            self._render_update_steps(modules)
            self.log_heading.set_text(_("Update log"))
        if state.running and not self.preview:
            text = N_("Rolling back…") if state.operation == "rollback" else N_("Bazzite is updating…")
        else:
            text = N_("Update Bazzite and Apps")
        detail = state.stage if state.running and state.stage else ""
        if state.running and not self.preview:
            active_modules = [module for module in modules
                              if isinstance(module, dict) and module.get("status") == "running"]
            active = next((module for module in active_modules
                           if state.stage.casefold() in {
                               str(module.get("id", "")).casefold(),
                               str(module.get("name", "")).casefold()}),
                          active_modules[0] if active_modules else None)
            if active is not None:
                identity = str(active.get("id") or active.get("name") or N_("Update step"))
                row = self._update_step_rows.get(identity)
                if row is not None:
                    detail = " • ".join(filter(None, (row.get_title(), row.get_subtitle())))
        if state.running and error_text(state.error, N_('Details are unavailable. Open Logs for more information.'), context='_render_status'):
            detail = (detail + " · " if detail else "") + error_text(state.error, N_('Details are unavailable. Open Logs for more information.'), context='_render_status')
        self.home_status.set_title(format_message(text))
        self.home_status.set_subtitle(format_message(format_message(detail) if detail else ("Commands are disabled in preview mode." if self.preview else _("Update your system and installed apps."))))
        errors = []
        transaction_active = self._deployment_transaction_active()
        self.update_transaction_notice.set_visible(transaction_active and not state.running and not self.preview)
        if error_text(state.error, N_('Details are unavailable. Open Logs for more information.'), context='_render_status') and state.result != "failed":
            errors.append(error_text(state.error, N_('Details are unavailable. Open Logs for more information.'), context='_render_status'))
        if state.deployment and not state.deployment_known:
            errors.append(N_("A system change is pending, but Bazzite Portal could not verify it. Restart is unavailable until its status can be checked."))
        if not self.preview and self.status_received and not state.available:
            errors.append(N_("Bazzite updates are unavailable. Open Logs for details."))
        self.update_error.set_text("\n".join(public_error(error, N_("Could not read update status. Open Logs for details."), context='Update status') for error in filter(None, errors)))
        self.update_error.set_visible(bool(errors) and not self.preview)
        reboot_ready = bool(state.deployment and state.deployment_known and not state.running)
        request_pending = getattr(self, "_update_request_pending", False)
        self.update_button.set_label(format_message(_("Updating…") if state.running or request_pending else _("Reboot") if reboot_ready else _("Update")))
        self.update_button.set_visible(True)
        self.update_button.set_sensitive(not self.preview and self.status_received and state.available
            and not state.running and not request_pending and not transaction_active and (not state.deployment or state.deployment_known)
            and state.cancel_state != "running" and state.layer_state != "running")
        if self.preview:
            self.update_button.set_visible(True)
            self.update_button.set_sensitive(self._preview_timer is None)
        self._sync_rollback_ui()
        self._sync_cancel_pending_ui()
        self._sync_channel_ui()
        self._sync_delete_deployments_ui()
        self._sync_image_ui()
        if self.preview and self._preview_log_lines:
            self._set_log_text("\n".join(self._preview_log_lines), follow_tail=True)
        else:
            self._set_log_text("" if historical_run or awaiting_run or dismissed_result else "\n".join(state.logs), follow_tail=True)
        self._refresh_update_prompts()
        self._update_controller_legend()
        self._render_home_activity()

    def _operation(self, method):
        if self.preview:
            self.toast("System operations are disabled in preview mode.")
        elif method == "StartUpdate":
            def rejected(error):
                if self.closing:
                    return
                self._update_request_pending = False
                self._awaiting_update_run = None
                self._render_status()
                self.toast(public_error(error, N_("The update request could not be sent. Open Logs for details."), context='Start update'))
            try:
                if self.client is None or getattr(self.client, "closed", False):
                    rejected(N_("System status is unavailable. Open Logs for details."))
                else:
                    self.client.call(method, error_callback=rejected)
            except Exception as error:
                rejected(error)
        elif self.client:
            self.client.call(method)

    def confirm(self, title, body, action, callback, *, informational=False, documentation_url=None):
        dialog = Adw.Window(transient_for=self, modal=True, title=format_message(title), default_width=480)
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        set_window_content(dialog, toolbar)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(24)
        toolbar.set_content(box)
        box.append(label(body))
        if documentation_url:
            documentation = Gtk.Label(xalign=0, wrap=True)
            documentation.set_markup('<a href="' + GLib.markup_escape_text(documentation_url) + '">'
                                     + GLib.markup_escape_text(_("Read the rebase documentation")) + '</a>')
            documentation.connect("activate-link", lambda _label, uri: (
                self._run_xdg_open(uri, N_("Could not open link")), True)[1])
            box.append(documentation)
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self.confirmation_prompts = [
            make_button_prompt(command, labels, action if command == "confirm" else N_("Cancel"), self._prompt_style())
            for command in (("confirm",) if informational else ("confirm", "back"))
        ]
        self.confirmation_prompt_labels = (action,) if informational else (action, N_("Cancel"))
        for prompt in self.confirmation_prompts:
            set_prompt_visible(prompt, self._prompts_visible())
        choices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        actions = [(action, callback, self.confirmation_prompts[0])]
        if not informational:
            actions.append((N_("Cancel"), None, self.confirmation_prompts[1]))
        for text, callback_fn, prompt in actions:
            choice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            choice.append(prompt)
            control = button(text, lambda cb=callback_fn: (dialog.close(), cb() if cb else None))
            control.controller_exclude_focus = True
            control.add_css_class("confirmation-choice")
            if text == action:
                control.add_css_class("suggested-action" if informational else "destructive-action")
                dialog.confirm_yes_button = control
            else:
                dialog.confirm_no_button = control
            choice.append(control)
            choices.append(choice)
        box.append(choices)
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        dialog.connect("close-request", lambda *_: self._clear_confirm_modal(dialog))
        dialog.present()
        self._update_controller_legend()

    def _clear_confirm_modal(self, dialog):
        if self.modal is dialog:
            self.modal = None
            self.confirmation_prompts = []
            self.confirmation_prompt_labels = (_("Yes"), _("No"))
        self._update_controller_legend()
        return False

    def _add_system_refresh_heading(self, box, title, prefix):
        box.remove(box.get_first_child())
        description = box.get_first_child()
        box.remove(description)
        heading = Gtk.Box(spacing=6)
        hint = make_context_prompt("focus_search", accessible_name=N_("Refresh system information"))
        heading.append(hint)
        refresh = button(N_("Refresh"), self._refresh_system, "view-refresh-symbolic")
        refresh.controller_exclude_focus = True
        refresh.set_tooltip_text(_("Refresh system information"))
        heading.append(refresh)
        if description.get_text():
            heading_block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            heading_block.append(description)
            box.prepend(heading_block)
        self.header.pack_end(heading)
        heading.set_visible(False)
        setattr(self, prefix + "_refresh_header", heading)
        setattr(self, prefix + "_refresh_hint", hint)
        setattr(self, prefix + "_refresh_button", refresh)

    def _set_system_refresh_busy(self, busy):
        for control in (self.system_refresh_button, self.management_refresh_button):
            control.set_sensitive(not busy)
        for spinner in self.system_section_spinners:
            spinner.set_visible(busy)
            spinner.start() if busy else spinner.stop()

    def _start_system_prefetch(self):
        if not self.closing and not self.system_has_snapshot:
            self._refresh_system()
        return GLib.SOURCE_REMOVE

    def _build_system(self):
        page, box = content_page(N_("System information"), "")
        management_page, management_box = content_page(N_("System management"), "")
        self.management_box = management_box
        self._add_system_refresh_heading(box, N_("System information"), "system")
        self._add_system_refresh_heading(management_box, N_("System management"), "management")
        self.system_group = group(N_("Bazzite Image Version"), N_("The image and update track used by your running system."))
        self.system_rows = {}
        for title in (N_("Image"), N_("Release channel")):
            row = Adw.ActionRow(title=_(title), subtitle=_("Loading…"))
            row.set_subtitle_lines(3)
            self.system_group.add(row)
            self.system_rows[title] = row
        box.append(self.system_group)
        self.deployment_group = group(N_("Deployment overview"), N_("Read-only details for the running and next-restart deployments."))
        self.deployment_status_row = Adw.ActionRow(title=_("Next restart"), subtitle=_("Loading next-restart information…"))
        self.deployment_status_row.set_sensitive(False)
        self.deployment_status_row.set_subtitle_lines(3)
        self.deployment_rows_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.deployment_rows_box.add_css_class("boxed-list")
        self.deployment_rows_box.append(self.deployment_status_row)
        self.deployment_group.add(self.deployment_rows_box)
        box.append(self.deployment_group)
        self.customization_group = group(N_("System customizations"), N_("Packages layered onto the system you are running now."))
        self.customization_rows_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.customization_rows_box.add_css_class("boxed-list")
        self.customization_group.add(self.customization_rows_box)
        self.customization_status_row = Adw.ActionRow(title=_("Customization details not loaded"))
        self.customization_group.add(self.customization_status_row)
        box.append(self.customization_group)
        self.system_section_spinners = []
        for section in (self.system_group, self.deployment_group, self.customization_group):
            spinner = Gtk.Spinner()
            spinner.set_size_request(20, 20)
            spinner.set_visible(False)
            section.set_header_suffix(spinner)
            self.system_section_spinners.append(spinner)
        self.system_loading = False
        self.system_has_snapshot = False
        self.deployment_summary = None
        self.deployment_summary_fresh = False
        self._rollback_request_pending = False
        self.cancel_target = None
        self.cancel_target_fresh = False
        self.cancel_request_active = False
        self.cancel_waiting_snapshot = False
        self.channel_status = None
        self.channel_status_fresh = False
        self.channel_request_active = False
        self.channel_poll_source = None
        self.channel_waiting_status = False
        self.channel_status_polls = 0
        self.image_status = None
        self.image_deployment_status = None
        self.image_request_active = False
        self.cancel_feedback = None
        self.cancel_refresh_after_system_load = False
        self.cache_request_active = False
        self.cache_feedback = None
        self.maintenance_acknowledged = set()
        self.maintenance_acknowledgement_pending = set()
        self.maintenance_expected_units = {}
        self.maintenance_pending_units = {}

        recovery = group(N_("Recovery"), N_("Rollback selects the previous system deployment for your next boot. Personal files are preserved."))
        self.recovery_group = recovery
        self.rollback_row = Adw.ActionRow(
            title=_("Checking previous deployment…"),
            subtitle=_("Refresh system information to check rollback availability."),
        )
        self.rollback_row.controller_focus_row = True
        self.rollback_row.set_activatable(True)
        self.rollback_row.connect("activated", lambda *_: self._begin_rollback())
        recovery.add(self.rollback_row)
        management_box.append(recovery)

        self.cancel_pending_group = group(N_("Manage Deployments"), N_("Manage prepared updates and saved system versions."))
        self.cancel_pending_row = Adw.ActionRow(title=_("Checking pending deployment…"))
        self.cancel_pending_row.controller_focus_row = True
        self.cancel_pending_row.set_activatable(True)
        self.cancel_pending_row.connect("activated", lambda *_: self._confirm_cancel_pending())
        self.cancel_pending_group.add(self.cancel_pending_row)
        self.clear_cache_row = Adw.ActionRow(
            title=_("Clear download cache"),
            subtitle=_("Remove cached update data. Future updates may download it again."),
        )
        self.clear_cache_row.controller_focus_row = True
        self.clear_cache_row.set_activatable(True)
        self.clear_cache_row.connect("activated", lambda *_: self._confirm_clear_download_cache())
        self.cancel_pending_group.add(self.clear_cache_row)
        self.cache_details_row = Adw.ActionRow(
            title=_("Cache clearing details"), subtitle=_("Read the operation log without opening a terminal.")
        )
        self.cache_details_row.controller_focus_row = True
        self.cache_details_row.set_activatable(True)
        self.cache_details_row.connect("activated", lambda *_: self._show_maintenance_log("cache"))
        self.cache_details_row.set_visible(False)
        self.cancel_status_row = Adw.ActionRow(title=_("Cancellation status unavailable"))
        self.cancel_status_row.set_subtitle_lines(2)
        self.cancel_status_row.set_visible(False)
        self.cancel_pending_group.add(self.cancel_status_row)
        self.cancel_details_row = Adw.ActionRow(title=_("Cancellation details"), subtitle=_("Read the operation log without opening a terminal."))
        self.cancel_details_row.controller_focus_row = True
        self.cancel_details_row.set_activatable(True)
        self.cancel_details_row.connect("activated", lambda *_: self._show_maintenance_log("cancel"))
        self.cancel_details_row.set_visible(False)
        management_box.append(self.cancel_pending_group)

        self.delete_pinned_deployments_row = Adw.ActionRow(
            title=_("Delete pinned deployments"),
            subtitle=_("Choose a saved, pinned system version to delete."),
        )
        self.delete_pinned_deployments_row.controller_focus_row = True
        self.delete_pinned_deployments_row.controller_read_only_focus = True
        self.delete_pinned_deployments_row.set_activatable(True)
        self.delete_pinned_deployments_row.connect("activated", lambda *_: self._show_deployment_selection())
        self.cancel_pending_group.add(self.delete_pinned_deployments_row)
        self.delete_status = None
        self.delete_status_fresh = False
        self.delete_request_active = False

        self.channel_group = group(N_("Release channel"), N_("Follow the latest Stable or Testing release of your Bazzite image."))
        self.channel_switch_row = Adw.ActionRow(
            title=_("Checking release channel…"), subtitle=_("Refresh system information to check availability.")
        )
        self.channel_switch_row.controller_focus_row = True
        self.channel_switch_row.controller_read_only_focus = True
        self.channel_switch_row.set_activatable(True)
        self.channel_switch_row.connect("activated", lambda *_: self._confirm_channel_switch())
        shield = app_icon("shield", 20, N_("Requires administrator authorization"))
        shield.set_tooltip_text(_("Requires administrator authorization"))
        self.channel_switch_row.add_prefix(shield)
        self.channel_group.add(self.channel_switch_row)
        self.channel_details_row = Adw.ActionRow(title=_('Release channel rebase details'), subtitle=_("View progress and the operation log."))
        self.channel_details_row.controller_focus_row = True
        self.channel_details_row.set_activatable(True)
        self.channel_details_row.connect("activated", lambda *_: self._show_maintenance_log("channel"))
        self.channel_details_row.set_visible(False)
        self.channel_group.add(self.channel_details_row)
        self.channel_specific_row = Adw.ActionRow(
            title=_('Rebase to specific image'), subtitle=_("Choose a Bazzite image, release channel and version.")
        )
        self.channel_specific_row.controller_focus_row = True
        self.channel_specific_row.add_prefix(app_icon("shield", 20, N_("Requires administrator authorization")))
        self.channel_specific_row.controller_read_only_focus = True
        self.channel_specific_row.set_activatable(True)
        self.channel_specific_row.connect("activated", lambda *_: self._show_image_selection())
        self.channel_group.add(self.channel_specific_row)
        self.image_details_row = Adw.ActionRow(title=_('Image rebase details'), subtitle=_("View progress and the operation log."))
        self.image_details_row.controller_focus_row = True
        self.image_details_row.set_activatable(True)
        self.image_details_row.connect("activated", lambda *_: self._show_maintenance_log("image"))
        self.image_details_row.set_visible(False)
        self.channel_group.add(self.image_details_row)
        management_box.append(self.channel_group)

        layers_group = group(N_("Remove layered apps"), N_("Remove system layers while keeping your personal files and other apps."))
        self.layers_group = layers_group
        self.layers_status = Adw.ActionRow(title=_("Checking layered apps…"))
        self.layers_busy_spinner = Gtk.Spinner()
        self.layers_busy_spinner.set_size_request(18, 18)
        self.layers_busy_spinner.set_visible(False)
        self.layers_status.add_suffix(self.layers_busy_spinner)
        self.layers_row = Adw.ActionRow(title=_("Choose layered packages to remove"), subtitle=_("Choose which layered packages to remove. A restart is needed to apply the change."))
        self.layers_row.controller_focus_row = True
        self.layers_row.set_activatable(True)
        self.layers_row.connect("activated", lambda *_: self._confirm_remove_layers())
        layers_group.add(self.layers_row)
        self.reset_layers_row = Adw.ActionRow(
            title=_("Remove all layered apps"),
            subtitle=_("Remove all system layers. Your files, Bazzite app store apps, Homebrew apps and AppImages stay."),
        )
        self.reset_layers_row.controller_focus_row = True
        self.reset_layers_row.set_activatable(True)
        self.reset_layers_row.connect("activated", lambda *_: self._confirm_reset_layers())
        layers_group.add(self.reset_layers_row)
        self.layer_details_row = Adw.ActionRow(title=_("Layer removal details"), subtitle=_("Read the operation log without opening a terminal."))
        self.layer_details_row.controller_focus_row = True
        self.layer_details_row.set_activatable(True)
        self.layer_details_row.connect("activated", lambda *_: self._show_maintenance_log("layer"))
        self.layer_details_row.set_visible(False)
        layers_group.add(self.layer_details_row)
        management_box.append(layers_group)
        self.layers_target = None
        self.layers_request_active = False
        self.layers_feedback = ""
        self.layers_feedback_is_error = False
        for row in (self.rollback_row, self.cancel_pending_row, self.clear_cache_row, self.delete_pinned_deployments_row,
                    self.layers_row, self.reset_layers_row):
            shield = app_icon("shield", 20, N_("Requires administrator authorization"))
            shield.set_tooltip_text(format_message(N_("Requires administrator authorization")))
            row.add_prefix(shield)
            row.controller_read_only_focus = True
        self.stack.add_named(page, N_("System"))
        self.stack.add_named(management_page, N_("Management"))
        if self.preview:
            self.delete_pinned_deployments_row.set_sensitive(False)
            self.delete_pinned_deployments_row.set_activatable(False)
            for row in self.system_rows.values():
                row.set_subtitle("Open this page to load system information.")
            self.deployment_status_row.set_title("Deployment information not loaded yet")
            self.deployment_status_row.set_subtitle("Read-only information is available in preview mode.")
            self.customization_status_row.set_title("System customizations not loaded yet")
            self.layers_row.set_sensitive(False)
            self.layers_row.set_activatable(False)
            self.reset_layers_row.set_sensitive(False)
            self.reset_layers_row.set_activatable(False)
            self.layers_status.set_title("Preview mode · removal disabled")
            self.cancel_pending_row.set_sensitive(False)
            self.cancel_pending_row.set_activatable(False)
            self.cancel_pending_row.set_title("Cancellation disabled in preview")
            self.cancel_pending_row.set_subtitle("Preview can read system information but cannot change deployments.")
            self.channel_switch_row.set_sensitive(False)
            self.channel_switch_row.set_activatable(False)
            self.channel_switch_row.set_title('Release channel rebasing disabled in preview')
            self.channel_switch_row.set_subtitle("Preview can read system information but cannot change deployments.")

    @staticmethod
    def _short_deployment_text(value, limit):
        text = str(value).strip() if value is not None else ""
        if not text:
            return N_("Unavailable")
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _clear_deployment_rows(self):
        while child := self.deployment_rows_box.get_first_child():
            self.deployment_rows_box.remove(child)
        self.deployment_rows_box.append(self.deployment_status_row)

    def _render_deployment_summary(self, summary):
        if not isinstance(summary, dict):
            summary = {"known": False, "error": "Deployment summary is malformed.", "entries": []}
        self.deployment_summary = summary
        known = summary.get("known") is True
        error = error_text(summary.get("error"), N_('Details are unavailable. Open Logs for more information.'), context='_render_deployment_summary')
        if not known:
            self._clear_deployment_rows()
            self.deployment_status_row.set_title(_("Next restart"))
            self.deployment_status_row.set_sensitive(False)
            self.deployment_status_row.set_subtitle(
                GLib.markup_escape_text(format_message(str(error or N_("The system did not report a recognizable deployment state."))))
            )
            return

        entries = summary.get("entries")
        if not isinstance(entries, list):
            entries = []
        role_order = {"Running now": 0, "Next restart": 1, "Saved deployment": 2}
        entries = sorted(entries, key=lambda item: role_order.get(item.get("role"), 3)
                         if isinstance(item, dict) else 3)
        self._clear_deployment_rows()
        next_entry = next((entry for entry in entries if isinstance(entry, dict) and entry.get("role") == N_("Next restart")), None)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("role") == N_("Next restart"):
                continue
            role = self._short_deployment_text(entry.get("role"), 48)
            version = self._short_deployment_text(entry.get("version"), 40)
            subtitle = _('Version {version}').format(version=version)
            labels = []
            if entry.get("pinned") is True:
                labels.append(N_("Pinned"))
            if entry.get("staged") is True:
                labels.append(N_("Prepared"))
            if labels:
                subtitle += " · " + " · ".join(labels)
            row = Adw.ActionRow(title=GLib.markup_escape_text(role),
                                subtitle=GLib.markup_escape_text(subtitle))
            row.set_subtitle_lines(3)
            row.set_activatable(False)
            row.set_selectable(False)
            self.deployment_rows_box.append(row)

        next_same = summary.get("next_boot_same")
        if next_same is True:
            next_status = N_("Your current system will start again. No system change is pending.")
        elif next_same is False:
            next_status = (_("Version {version}").format(version=self._short_deployment_text(next_entry.get("version"), 40))
                           if next_entry else N_("Next restart version is unavailable."))
        else:
            next_status = N_("Next restart target is unknown.")
        transaction = summary.get("transaction")
        status_parts = [next_status]
        if transaction:
            if isinstance(transaction, (dict, list)):
                transaction = json.dumps(transaction, ensure_ascii=False, separators=(",", ":"))
            status_parts.append(_("Transaction: {transaction}").format(transaction=self._short_deployment_text(transaction, 100)))
        self.deployment_status_row.set_title(_("Next restart"))
        self.deployment_status_row.set_sensitive(bool(next_same is False and next_entry))
        self.deployment_status_row.set_subtitle(GLib.markup_escape_text(format_message(" ".join(status_parts))))
        self._sync_rollback_ui()

    def _rollback_eligibility(self):
        summary = self.deployment_summary if isinstance(self.deployment_summary, dict) else {}
        entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
        saved = next((entry for entry in entries if isinstance(entry, dict)
                      and entry.get("role") == N_("Saved deployment")), None)
        next_restart = next((entry for entry in entries if isinstance(entry, dict)
                             and entry.get("role") == N_("Next restart")), None)
        if self.preview:
            return saved, "Rollback is disabled in preview mode."
        if self.system_loading or not self.deployment_summary_fresh:
            return saved, N_("Checking previous deployment… Refresh system information to check rollback availability.")
        if not summary.get("known"):
            return saved, N_("Previous deployment availability is unknown. Refresh system information.")
        pending_rollback = (
            summary.get("engine") == "rpm-ostree"
            and len(entries) == 2
            and isinstance(next_restart, dict)
            and next_restart.get("staged") is False
            and isinstance(entries[1], dict)
            and entries[1].get("role") == N_("Running now")
        )
        if pending_rollback:
            saved = next_restart
        if saved is None:
            if (summary.get("engine") == "bootc" and isinstance(next_restart, dict)
                    and next_restart.get("staged") is False):
                return None, N_("A rollback is already prepared for the next restart.")
            return None, N_("No previous deployment is available.")
        identity = saved.get("identity")
        if not isinstance(identity, str) or not identity.strip() or identity.strip().lower() == "unknown":
            return saved, public_error('The saved deployment identity is unavailable. Refresh system information.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        if isinstance(next_restart, dict) and not pending_rollback:
            return saved, N_("A prepared deployment is waiting for restart. Cancel it before rolling back.")
        if not self.status_received or self.client is None or getattr(self.client, "closed", False):
            return saved, N_("System status is unavailable. Open Logs for details.")
        blocker = self._management_blocker_reason("rollback")
        if blocker:
            return saved, blocker
        return saved, ""

    def _sync_rollback_ui(self):
        if not hasattr(self, "rollback_row"):
            return
        state = getattr(self.snapshot, "rollback_state", "unavailable")
        logs = getattr(self.snapshot, "rollback_logs", [])
        detail = error_text(getattr(self.snapshot, "rollback_error", ""), N_('Could not check rollback status. Open Logs for details.'), context='_sync_rollback_ui') or N_("Open to view operation progress and logs.")
        if (state == "running" or self._maintenance_row_should_open("rollback")):
            if state == "running":
                title = N_("Rolling back…")
            elif state == "success":
                title = N_("Rollback succeeded")
            elif state == "failed":
                title = N_("Rollback failed")
            else:
                title = N_("Rollback status unknown")
            self.rollback_row.set_title(format_message(title))
            self.rollback_row.set_subtitle(GLib.markup_escape_text(format_message(format_message(detail) if detail else _("Open to view operation progress and logs."))))
            self._sync_maintenance_row("rollback")
            return
        saved, reason = self._rollback_eligibility()
        enabled = saved is not None and not reason
        pending_rollback = bool(enabled and saved.get("role") == N_("Next restart"))
        title = (N_("Cancel pending rollback") if pending_rollback else
                 N_("Roll back to previous deployment") if enabled else
                 N_("Rollback already prepared") if reason.startswith(N_("A rollback is already prepared")) else
                 N_("No previous deployment available") if reason == N_("No previous deployment is available.") else
                 N_("Checking previous deployment…") if self.system_loading or not self.deployment_summary_fresh
                 or not (self.deployment_summary or {}).get("known") else
                 N_("Roll back to previous deployment"))
        if enabled:
            version = self._short_deployment_text(saved.get("version"), 48)
            subtitle = (N_("Keep your current system for the next restart. The saved version remains available.")
                        if pending_rollback else
                        _('Version {version} · Takes effect after restart. Personal files are preserved.').format(version=version))
        elif reason == N_("No previous deployment is available."):
            subtitle = N_("Restore the previous system version on the next boot. Personal files are preserved.")
        else:
            subtitle = reason or N_("Rollback is unavailable.")
        self.rollback_row.set_title(_(title))
        self.rollback_row.set_subtitle(format_message(subtitle))
        self.rollback_row.set_sensitive(enabled)
        self.rollback_row.set_activatable(enabled)
        self._sync_maintenance_row("rollback")

    def _maintenance_row_should_open(self, kind, operation=None):
        if kind == "layer" and operation:
            unit = (getattr(self, "maintenance_pending_units", {}).get(kind, {}).get("unit")
                    or getattr(self, "maintenance_expected_units", {}).get(kind)
                    or getattr(self.snapshot, "layer_unit", ""))
            is_reset = unit.startswith(("bazzite-portal-maintenance@reset-layers-", "bazzite-portal-reset-layers@"))
            if (operation == "reset") != is_reset:
                return False
        if (getattr(self, kind + "_request_active", False)
                or kind in getattr(self, "maintenance_pending_units", {})):
            return True
        state = getattr(self.snapshot, kind + "_state", "unavailable")
        unit = getattr(self.snapshot, kind + "_unit", "")
        invocation = getattr(self.snapshot, kind + "_invocation", "")
        key = self._maintenance_key(kind, unit, invocation)
        if not unit:
            return False
        if state == "running":
            return True
        # A prior successful cancellation can remain in the agent snapshot
        # after a different deployment has been prepared. Keep the live
        # cancellation action available instead of reopening that old result.
        if kind == "cancel" and state == "success":
            # Read the shared backend target directly. Eligibility itself uses
            # the row's lifecycle when checking shared blockers; calling it
            # here creates a recursive eligibility -> lifecycle -> eligibility
            # loop whenever an old cancellation and a new target coexist.
            target = getattr(self, "cancel_target", None)
            if (getattr(self, "cancel_target_fresh", False)
                    and isinstance(target, dict) and target.get("available") is True):
                return False
        return state in {"success", "failed", "unknown"} and (kind, *key) not in getattr(self, "maintenance_acknowledged", set())

    def _open_maintenance_if_current(self, kind, operation=None):
        if self._maintenance_row_should_open(kind, operation):
            expected = getattr(self, "maintenance_expected_units", {}).get(kind)
            if (expected == getattr(self.snapshot, kind + "_unit", "")
                    and getattr(self.snapshot, kind + "_state", "") in {"running", "success", "failed", "unknown"}):
                expected = None
            self._show_maintenance_log(kind, expected)
            return True
        return False

    def _clear_maintenance_pending(self, kind):
        self.maintenance_pending_units.pop(kind, None)
        self.maintenance_expected_units.pop(kind, None)
        self._sync_maintenance_row(kind)

    def _maintenance_key(self, kind, unit=None, invocation=None):
        unit = getattr(self.snapshot, kind + "_unit", "") if unit is None else unit
        invocation = getattr(self.snapshot, kind + "_invocation", "") if invocation is None else invocation
        generations = getattr(self.snapshot, "maintenance_generations", {}) or {}
        generation = generations.get(kind, 0) if isinstance(generations, dict) else 0
        try:
            generation = int(generation)
        except (TypeError, ValueError):
            generation = 0
        return unit, invocation, generation

    def _sync_management_operation_row(self, row, kind, *, request_active=False, allow_open=True):
        """Keep operation progress on its original action row."""
        if row is None:
            return
        state = getattr(self.snapshot, kind + "_state", "unavailable")
        unit = getattr(self.snapshot, kind + "_unit", "")
        invocation = getattr(self.snapshot, kind + "_invocation", "")
        acknowledged = (kind, *self._maintenance_key(kind, unit, invocation)) in getattr(self, "maintenance_acknowledged", set())
        pending = kind in getattr(self, "maintenance_pending_units", {})
        busy = request_active or allow_open and pending or allow_open and state == "running"
        spinner = getattr(row, "maintenance_spinner", None)
        if spinner is None:
            spinner = Gtk.Spinner()
            spinner.set_size_request(18, 18)
            row.maintenance_spinner = spinner
            indicator = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE,
                                  valign=Gtk.Align.CENTER)
            indicator.add_named(spinner, "running")
            for result, icon, css in (("success", "circle-check", "success"),
                                      ("failed", "circle-x", "error"),
                                      ("unknown", "circle-help", "dim-label")):
                child = app_icon(icon, 18, _("View operation result"))
                child.add_css_class(css)
                indicator.add_named(child, result)
            row.add_suffix(indicator)
            row.maintenance_indicator = indicator
        unread = allow_open and self._maintenance_row_should_open(kind) and state in {"success", "failed", "unknown"}
        spinner.set_visible(busy)
        row.maintenance_indicator.set_visible(busy or unread)
        row.maintenance_indicator.set_visible_child_name("running" if busy else state if unread else "running")
        spinner.start() if busy else spinner.stop()
        for css_class in ("success", "error"):
            row.remove_css_class(css_class)
        can_reopen = bool(allow_open and (request_active or unit and (state == "running" or
                                    state in {"success", "failed", "unknown"} and not acknowledged))
                          )
        if can_reopen:
            row.set_sensitive(True)
            row.set_activatable(True)

    def _sync_maintenance_row(self, kind):
        pending = getattr(self, "maintenance_pending_units", {}).get(kind)
        if pending:
            unit = getattr(self.snapshot, kind + "_unit", "")
            invocation = getattr(self.snapshot, kind + "_invocation", "")
            state = getattr(self.snapshot, kind + "_state", "")
            generation = self._maintenance_key(kind, unit, invocation)[2]
            new_generation = generation > pending["previous_generation"]
            fresh_invocation = bool(new_generation and invocation and invocation != pending["previous_invocation"])
            authoritative_terminal_without_invocation = state in {"failed", "unknown"} and not invocation
            if unit == pending["unit"] and (fresh_invocation or new_generation and authoritative_terminal_without_invocation):
                self.maintenance_pending_units.pop(kind, None)
                self.maintenance_expected_units.pop(kind, None)
        details = getattr(self, kind + "_details_row", None)
        if details is not None:
            details.set_visible(False)
        if kind == "rollback":
            rows = (getattr(self, "rollback_row", None),)
            request_active = getattr(self, "rollback_request_active", False) or kind in self.maintenance_pending_units
        elif kind == "cancel":
            rows = (getattr(self, "cancel_pending_row", None),)
            request_active = getattr(self, "cancel_request_active", False) or kind in self.maintenance_pending_units
        elif kind == "cache":
            rows = (getattr(self, "clear_cache_row", None),)
            request_active = getattr(self, "cache_request_active", False) or kind in self.maintenance_pending_units
        elif kind == "layer":
            rows = (getattr(self, "layers_row", None), getattr(self, "reset_layers_row", None))
            request_active = getattr(self, "layers_request_active", False) or kind in self.maintenance_pending_units
            operation_unit = (self.maintenance_pending_units.get(kind, {}).get("unit")
                              or self.maintenance_expected_units.get(kind)
                              or getattr(self.snapshot, "layer_unit", ""))
            active_row = (1 if operation_unit.startswith(("bazzite-portal-maintenance@reset-layers-", "bazzite-portal-reset-layers@")) else 0)
        elif kind == "channel":
            rows = (getattr(self, "channel_switch_row", None),)
            request_active = getattr(self, "channel_request_active", False) or kind in self.maintenance_pending_units
        elif kind == "image":
            rows = (getattr(self, "channel_specific_row", None),)
            request_active = getattr(self, "image_request_active", False) or kind in self.maintenance_pending_units
        elif kind == "delete":
            rows = (getattr(self, "delete_pinned_deployments_row", None),)
            request_active = getattr(self, "delete_request_active", False) or kind in self.maintenance_pending_units
        else:
            return
        for index, row in enumerate(rows):
            allow_open = kind != "layer" or index == active_row
            self._sync_management_operation_row(
                row, kind, request_active=request_active if allow_open else False,
                allow_open=allow_open)

    def _request_rollback(self):
        if self.preview or self.closing:
            return
        if self.client is None or getattr(self.client, "closed", False):
            self.toast(N_("System status is unavailable. Open Logs for details."))
            return
        self.rollback_request_active = True
        expected_unit = "bazzite-portal-maintenance@rollback.service"
        self._sync_maintenance_row("rollback")
        self._show_maintenance_log("rollback", expected_unit)

        def finished(error=None):
            self.rollback_request_active = False
            if error:
                self._clear_maintenance_pending("rollback")
            dialog = self.modal
            if dialog is not None and getattr(dialog, "operation_kind", "") == "rollback":
                dialog.request_completed = not error
                dialog.request_error = str(error) if error else ""
                self._sync_maintenance_dialog()
            self._sync_rollback_ui()
            if error:
                self.toast(public_error(error, N_("Rollback failed. Open Logs for details."), context='Rollback request'))

        try:
            self.client.call("StartRollback", callback=lambda *_: finished(), error_callback=finished)
        except Exception as error:
            finished(error)

    def _begin_rollback(self):
        if self._open_maintenance_if_current("rollback"):
            return
        saved, reason = self._rollback_eligibility()
        if reason or saved is None:
            self.toast(reason or N_("Rollback is unavailable."))
            self._sync_rollback_ui()
            return
        if self.modal is not None:
            return
        version = self._short_deployment_text(saved.get("version"), 48)
        cancel_pending_rollback = saved.get("role") == N_("Next restart")
        expected_identity = saved.get("identity")
        def start_rollback():
            current, current_reason = self._rollback_eligibility()
            if (current_reason or current is None or current.get("role") != saved.get("role")
                    or current.get("identity") != expected_identity):
                self.toast(current_reason or N_("Rollback is unavailable."))
                self._sync_rollback_ui()
                return
            self._request_rollback()
        if cancel_pending_rollback:
            self.confirm(
                N_("Cancel pending rollback?"),
                _('Keep your current system for the next restart. The saved deployment, version {version}, will remain available.').format(version=version),
                N_("Cancel rollback"), start_rollback,
            )
        else:
            self.confirm(
                N_("Roll back Bazzite?"),
                _('Select the saved deployment, version {version}, for your next boot. A restart is required to use it. Your personal files are preserved.').format(version=version),
                N_("Rollback"), start_rollback,
            )

    def _cancel_pending_eligibility(self):
        target = self.cancel_target if isinstance(self.cancel_target, dict) else None
        state = self.snapshot
        if self.preview:
            return target, "Preview mode · deployment cancellation is disabled."
        if not self.status_received:
            return target, N_("Checking system status…")
        if self.client is None or getattr(self.client, "closed", False):
            return target, N_("System status is unavailable. Open Logs for details.")
        if self.cancel_waiting_snapshot:
            return target, N_("Waiting for confirmation that cancellation has started…")
        blocker = self._management_blocker_reason("cancel")
        if blocker:
            return target, blocker
        if state.cancel_state == "unknown":
            return target, error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_cancel_pending_eligibility') or N_("Cancellation status is unknown. Refresh update status before continuing.")
        if state.cancel_state == "unavailable" or not state.cancel_available:
            reason = error_text((target or {}).get("reason"), N_('Could not check the pending system change. Open Logs for details.'), context='_cancel_pending_eligibility') or error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_cancel_pending_eligibility')
            return target, str(reason or N_("Cancelling the pending system change is unavailable. Open Logs for details."))
        if self.system_loading or not self.cancel_target_fresh:
            return target, N_("Refresh the deployment overview before cancelling.")
        if target is None or target.get("available") is not True:
            reason = error_text((target or {}).get("reason"), N_('Could not check the pending system change. Open Logs for details.'), context='_cancel_pending_eligibility') or N_("No verified prepared deployment is eligible for cancellation.")
            return target, str(reason)
        if state.deployment_known is not True or not state.deployment:
            return target, public_error('The update agent has not verified a pending deployment identity.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        identity = target.get("identity")
        if not isinstance(identity, str) or identity != state.deployment:
            return target, public_error('The deployment overview and update-agent identity do not match. Refresh before cancelling.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        token = target.get("token")
        if not isinstance(token, str) or not token:
            return target, public_error('The pending deployment cancellation token is unavailable.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        return target, ""

    def _sync_cancel_pending_ui(self):
        if not hasattr(self, "cancel_status_row"):
            return
        state = self.snapshot
        target, reason = self._cancel_pending_eligibility()
        enabled = not reason and target is not None and target.get("available") is True
        actionable = enabled or self._maintenance_row_should_open("cancel")
        self.cancel_pending_row.set_sensitive(actionable)
        self.cancel_pending_row.set_activatable(actionable)
        self.cancel_pending_row.set_subtitle(_(reason))
        if self.preview:
            title, detail = "Preview mode · cancellation disabled", "System information is read-only in preview."
        elif self.cancel_request_active:
            title, detail = N_("Cancelling pending deployment…"), N_("Open to view operation progress and logs.")
        elif state.cancel_state == "running":
            title, detail = N_("Cancelling pending deployment…"), error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or N_("Open to view operation progress and logs.")
        elif state.cancel_state == "success":
            if target is not None and target.get("available") is True:
                title, detail = N_("Prepared deployment can be cancelled"), N_("Only the verified prepared next-restart deployment will be removed.")
            else:
                title, detail = N_("Pending deployment cancelled"), error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or N_("The running and saved deployments were left unchanged.")
        elif state.cancel_state == "failed":
            title, detail = N_("Deployment cancellation failed"), error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or N_("The operation failed. Refresh system information to check the current state.")
        elif self.cancel_waiting_snapshot:
            title, detail = N_("Cancellation request sent"), N_("Open to view operation progress and logs.")
        elif self.cancel_feedback:
            title, detail = self.cancel_feedback
        elif state.cancel_state == "unknown":
            title, detail = N_("Cancellation status unknown"), error_text(state.cancel_error, N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or reason
        elif state.cancel_state == "unavailable" or not state.cancel_available:
            title, detail = N_("Cancellation unavailable"), reason
        elif reason:
            title, detail = N_("Cancellation unavailable"), reason
        else:
            title = N_("Prepared deployment can be cancelled")
            detail = N_("Only the verified prepared next-restart deployment will be removed.")
        self.cancel_status_row.set_title(GLib.markup_escape_text(_(title)))
        self.cancel_status_row.set_subtitle(GLib.markup_escape_text(format_message(str(detail))))
        self.cancel_status_row.set_visible(False)
        absent_pending = (not self.preview and self.cancel_target_fresh and
                         isinstance(target, dict) and target.get("available") is False and
                         N_("There is no pending deployment") in str(target.get("reason", "")))
        if self.preview:
            action_title, action_detail = "Cancellation disabled in preview", "System information is read-only in preview."
        elif self.cancel_request_active:
            action_title, action_detail = title, detail
        elif state.cancel_state == "running":
            action_title, action_detail = title, detail
        elif state.cancel_state == "failed" and enabled:
            action_title = N_("Retry pending deployment cancellation")
            action_detail = N_("The previous attempt failed. Your pending deployment can still be cancelled.")
        elif (self.cancel_waiting_snapshot or self.cancel_feedback
              or state.cancel_state in {"success", "failed", "unknown"}
              and (state.cancel_state != "success" or self._maintenance_row_should_open("cancel"))):
            action_title, action_detail = title, detail
        elif state.cancel_state == "unavailable" or not state.cancel_available:
            action_title, action_detail = title, detail
        elif self.system_loading or not self.cancel_target_fresh:
            action_title, action_detail = N_("Checking pending deployment…"), reason or N_("Refresh deployment information to check cancellation.")
        elif absent_pending:
            action_title, action_detail = N_("No pending deployment to cancel"), str(error_text(target.get("reason"), N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or "")
        elif enabled:
            action_title, action_detail = N_("Cancel pending deployment"), N_("Cancel only the verified prepared deployment before it is activated.")
        else:
            action_title, action_detail = title, detail
        self.cancel_pending_row.set_title(GLib.markup_escape_text(_(action_title)))
        if absent_pending and state.cancel_state not in {"running", "success", "failed", "unknown"}:
            action_title = N_("No pending deployment to cancel")
            action_detail = str(error_text(target.get("reason"), N_('Could not check the pending system change. Open Logs for details.'), context='_sync_cancel_pending_ui') or "")
            self.cancel_pending_row.set_title(GLib.markup_escape_text(_(action_title)))
        busy = self.cancel_request_active or state.cancel_state == "running"
        if busy:
            action_detail = N_("Open to view operation progress and logs.")
        self._sync_maintenance_row("cancel")
        self.cancel_busy_spinner = self.cancel_pending_row.maintenance_spinner
        self.cancel_pending_row.set_subtitle(GLib.markup_escape_text(format_message(str(action_detail))))
        actionable = enabled or self._maintenance_row_should_open("cancel")
        self.cancel_pending_row.set_sensitive(actionable)
        self.cancel_pending_row.set_activatable(actionable)
        self._sync_rollback_ui()

    def _confirm_cancel_pending(self):
        if self._open_maintenance_if_current("cancel"):
            return
        target, reason = self._cancel_pending_eligibility()
        if reason or target is None:
            self.toast(reason or N_("Deployment cancellation is unavailable."))
            self._sync_cancel_pending_ui()
            return
        version = self._short_deployment_text(target.get("version"), 48)
        image = self._short_deployment_text(target.get("image"), 96)
        body = (
            _('Cancel the prepared Bazzite deployment {version} ({image})? This removes only the deployment prepared for the next restart. The running deployment, saved deployment, and personal files remain unchanged. This will not restart the computer.').format(version=version, image=image)
        )
        token = target["token"]
        self.confirm(N_("Cancel pending deployment?"), body, N_("Cancel deployment"),
                     lambda: self._request_cancel_pending(token))

    def _request_cancel_pending(self, expected_token):
        if self.preview or self.closing:
            return
        target, reason = self._cancel_pending_eligibility()
        if reason or target is None:
            self.toast(reason or N_("Deployment cancellation is unavailable."))
            self._sync_cancel_pending_ui()
            return
        if target.get("token") != expected_token:
            self.toast(N_("The pending deployment changed. Review the updated details and confirm again."))
            self.cancel_target_fresh = False
            self._sync_cancel_pending_ui()
            return

        self.cancel_request_active = True
        expected_unit = f"bazzite-portal-maintenance@cancel-pending-{expected_token}.service"
        self.cancel_waiting_snapshot = True
        self.cancel_target_fresh = False
        self.deployment_summary_fresh = False
        self.cancel_feedback = None
        self._sync_cancel_pending_ui()
        self._show_maintenance_log("cancel", expected_unit)

        def request_finished(_reply):
            if self.closing:
                return
            self.cancel_request_active = False
            self.cancel_waiting_snapshot = False
            self.cancel_feedback = None
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "cancel":
                self.modal.request_completed = True
                self._sync_maintenance_dialog()
            self._sync_cancel_pending_ui()
            if self.stack.get_visible_child_name() in {N_("System"), N_("Management")}:
                self._refresh_system()

        def request_failed(error):
            if self.closing:
                return
            self.cancel_request_active = False
            self.cancel_waiting_snapshot = False
            self._clear_maintenance_pending("cancel")
            self.cancel_feedback = None
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "cancel":
                self.modal.request_error = str(error) or N_("The cancellation request failed.")
                self._sync_maintenance_dialog()
            self.cancel_target_fresh = False
            self._sync_cancel_pending_ui()
            if self.stack.get_visible_child_name() in {N_("System"), N_("Management")}:
                self._refresh_system()

        try:
            self.client.call(
                "CancelPending", GLib.Variant("(s)", (expected_token,)),
                callback=request_finished, error_callback=request_failed,
            )
        except Exception as error:
            request_failed(error)

    def _refresh_system_after_cancel(self):
        if self.closing:
            return
        if self.system_loading:
            self.cancel_refresh_after_system_load = True
        elif self.stack.get_visible_child_name() in {N_("System"), N_("Management")}:
            self.cancel_refresh_after_system_load = False
            self._refresh_system()

    def _finish_deferred_cancel_refresh(self):
        if not self.cancel_refresh_after_system_load:
            return
        self.cancel_refresh_after_system_load = False
        if self.stack.get_visible_child_name() in {N_("System"), N_("Management")} and not self.closing:
            GLib.idle_add(lambda: (self._refresh_system(), GLib.SOURCE_REMOVE)[1])

    def _layers_reason(self):
        if self.preview:
            return "Preview mode · removal disabled."
        if self.client is None or getattr(self.client, "closed", False) or not self.status_received:
            return N_("Checking system status…")
        if self.system_loading or not self.cancel_target_fresh:
            return N_("Refresh system information before continuing.")
        blocker = self._management_blocker_reason("layer")
        if blocker:
            return blocker
        if not getattr(self.snapshot, "layer_available", False) or getattr(self.snapshot, "layer_state", "unknown") == "unknown":
            return N_("Layer removal is unavailable. Open Logs for details.")
        if not self.layers_target or not self.layers_target["available"]:
            reason = error_text((self.layers_target or {}).get("reason", N_("Package details are unavailable.")), N_('Could not check layered apps. Open Logs for details.'), context='_layers_reason')
            reason = reason.replace(N_("No requested package layers are present."), N_("No package layers are present."))
            if reason == N_("A different deployment is selected for next boot."):
                return N_("Apply or cancel the pending system change before removing layered apps.")
            return reason
        return ""

    def _sync_layers_ui(self):
        reason = self._layers_reason()
        self.layers_row.set_sensitive(not reason)
        self.layers_row.set_activatable(not reason)
        state = getattr(self.snapshot, "layer_state", "unavailable")
        detail = error_text(getattr(self.snapshot, "layer_error", ""), N_('Could not check layered apps. Open Logs for details.'), context='_sync_layers_ui')
        if self.layers_request_active or state == "running":
            text = N_("Removing layered apps…")
        elif self.layers_feedback_is_error:
            text = public_error(self.layers_feedback, N_("Layer removal failed. Open the operation log for details."), context='Layer removal')
        elif state == "failed":
            text = public_error(detail, N_("Layer removal failed. Open the operation log for details."), context='Layer removal')
        elif state == "success":
            text = N_("Layer removal completed.")
        else:
            count = len((self.layers_target or {}).get('packages', []))
            text = self.layers_feedback or reason or ngettext(
                "{count} package request to review.", "{count} package requests to review.", count).format(count=count)
        self.layers_status.set_title(GLib.markup_escape_text(format_message(text)))
        busy = state == "running" or self.layers_request_active
        self.layers_busy_spinner.set_visible(busy)
        self.layers_busy_spinner.start() if busy else self.layers_busy_spinner.stop()
        subtitle = (N_("Open to view operation progress and logs.")
                    if busy else public_error(self.layers_feedback, N_("Layer removal failed. Open the operation log for details."), context="Layer removal") if self.layers_feedback_is_error else
                    N_("Layer removal succeeded. Restart your system manually to apply the change.")
                    if state == "success" and self._maintenance_row_should_open("layer")
                    and self.snapshot.deployment and self.snapshot.deployment_known else "")
        self.layers_status.set_subtitle(GLib.markup_escape_text(format_message(subtitle)))
        if reason:
            status_subtitle = subtitle if busy else reason
            self.layers_row.set_subtitle(GLib.markup_escape_text(format_message(status_subtitle)))
            self.reset_layers_row.set_subtitle(GLib.markup_escape_text(format_message(status_subtitle)))
        else:
            self.layers_row.set_subtitle(_("Choose which layered packages to remove. A restart is needed to apply the change."))
            self.reset_layers_row.set_subtitle(_("Remove all system layers. Your files, Bazzite app store apps, Homebrew apps and AppImages stay."))
        self.reset_layers_row.set_sensitive(not reason)
        self.reset_layers_row.set_activatable(not reason)
        self._sync_rollback_ui()
        self._sync_maintenance_row("layer")

    def _cache_clear_reason(self):
        state = self.snapshot
        if state.management and state.management.get("known") is not True:
            return N_("System status is unavailable. Refresh system information before continuing.")
        if self.preview:
            return "Preview mode · clearing the download cache is disabled."
        if self.client is None or getattr(self.client, "closed", False) or not self.status_received:
            return N_("Checking system status…")
        blocker = self._management_blocker_reason("cache")
        if blocker:
            return blocker
        if not state.cache_available:
            return (error_text(getattr(state, "cache_reason", ""), N_('Could not check download-cache status. Open Logs for details.'), context='_cache_clear_reason') or error_text(state.cache_error, N_('Could not check download-cache status. Open Logs for details.'), context='_cache_clear_reason')
                    or N_("Download-cache cleanup is unavailable. Open Logs for details."))
        if state.cache_state in {"unknown", "unavailable"}:
            return error_text(state.cache_error, N_('Could not check download-cache status. Open Logs for details.'), context='_cache_clear_reason') or N_("Could not check download-cache status. Refresh system information and try again.")
        token = state.cache_token
        if (not isinstance(token, str) or len(token) != 64
                or any(char not in "0123456789abcdef" for char in token)):
            return public_error('The download-cache confirmation token is unavailable.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        return ""

    def _sync_clear_cache_ui(self):
        if not hasattr(self, "clear_cache_row"):
            return
        reason = self._cache_clear_reason()
        enabled = not reason
        state = self.snapshot
        if self.preview:
            title, detail = "Download cache clearing disabled in preview", "System information is read-only in preview."
        elif self.cache_request_active or state.cache_state == "running":
            title, detail = N_("Clearing download cache…"), N_("Open to view operation progress and logs.")
        elif state.cache_state == "success" and self._maintenance_row_should_open("cache"):
            title, detail = N_("Download cache cleared"), error_text(state.cache_error, N_('Could not check download-cache status. Open Logs for details.'), context='_sync_clear_cache_ui') or N_("The cached data was removed. It can be downloaded again when needed.")
        elif state.cache_state == "failed":
            title = N_("Retry clearing download cache") if enabled else N_("Could not clear download cache")
            detail = reason or error_text(state.cache_error, N_('Could not check download-cache status. Open Logs for details.'), context='_sync_clear_cache_ui') or N_("Download-cache cleanup failed. Open the operation log for details.")
        elif self.cache_feedback:
            title, detail = self.cache_feedback
            if enabled and title == N_("Could not clear download cache"):
                title = N_("Retry clearing download cache")
            elif reason:
                detail = reason
        elif reason:
            title, detail = N_("Download-cache clearing unavailable"), reason
        else:
            title, detail = N_("Clear download cache"), N_("Remove cached update data. Future updates may download it again.")
        self.clear_cache_row.set_title(GLib.markup_escape_text(format_message(title)))
        self.clear_cache_row.set_subtitle(GLib.markup_escape_text(format_message(detail)))
        self.clear_cache_row.set_sensitive(enabled)
        self.clear_cache_row.set_activatable(enabled)
        self._sync_maintenance_row("cache")

    def _confirm_clear_download_cache(self):
        if self._open_maintenance_if_current("cache"):
            return
        reason = self._cache_clear_reason()
        if reason:
            self.toast(reason)
            self._sync_clear_cache_ui()
            return
        token = self.snapshot.cache_token
        self.confirm(
            N_("Clear download cache?"),
            N_("This removes cached update data, which may need to be downloaded again later. If a deployment is pending, cancel it first with Cancel pending deployment; clearing the cache will not cancel it for you. No restart is needed."),
            N_("Clear cache"), lambda: self._request_clear_download_cache(token),
        )

    def _request_clear_download_cache(self, expected_token):
        if self.preview or self.closing:
            return
        if (self.cache_request_active or self.snapshot.cache_state == "running"
                or "cache" in self.maintenance_pending_units):
            self._open_maintenance_if_current("cache")
            return
        reason = self._cache_clear_reason()
        if reason:
            self.toast(reason)
            self._sync_clear_cache_ui()
            return
        if self.snapshot.cache_token != expected_token:
            self.toast(N_("The download cache changed. Review the updated details and confirm again."))
            self._sync_clear_cache_ui()
            return
        self.cache_request_active = True
        expected_unit = f"bazzite-portal-maintenance@clear-download-cache-{expected_token}.service"
        self.cache_feedback = None
        self._sync_clear_cache_ui()
        self._show_maintenance_log("cache", expected_unit)

        def finished(_reply):
            if self.closing:
                return
            self.cache_request_active = False
            self.cache_feedback = None
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "cache":
                self.modal.request_completed = True
                self._sync_maintenance_dialog()
            self._sync_clear_cache_ui()

        def failed(error):
            if self.closing:
                return
            self.cache_request_active = False
            self._clear_maintenance_pending("cache")
            self.cache_feedback = None
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "cache":
                self.modal.request_error = str(error) or N_("The cache cleanup request failed.")
                self._sync_maintenance_dialog()
            self._sync_clear_cache_ui()

        try:
            self.client.call(
                "ClearDownloadCache", GLib.Variant("(s)", (expected_token,)),
                callback=finished, error_callback=failed,
            )
        except Exception as error:
            failed(error)

    def _show_maintenance_log(self, kind, expected_unit=None):
        if self.preview or self.closing or self.modal:
            return
        unit = expected_unit or getattr(self.snapshot, kind + "_unit", "")
        if not unit:
            return
        if expected_unit:
            self.maintenance_expected_units[kind] = expected_unit
            self.maintenance_pending_units[kind] = {
                "unit": expected_unit,
                "previous_invocation": getattr(self.snapshot, kind + "_invocation", "")
                    if expected_unit == getattr(self.snapshot, kind + "_unit", "") else "",
                "previous_generation": self._maintenance_key(kind)[2],
            }
        dialog = Adw.Window(transient_for=self, modal=True, default_width=560,
                            title={"rollback": _("Rollback"), "cancel": _("Cancel pending deployment"),
                                   "layer": _("Remove layered apps"), "cache": _("Clear download cache"),
                                   "channel": _('Rebase release channel'), "image": _('Rebase Bazzite image'),
                                   "delete": _("Delete pinned deployment")}.get(kind, _("System operation")))
        dialog.set_application(self.get_application())
        dialog.operation_kind = kind
        dialog.previous_generation = self._maintenance_key(kind)[2]
        dialog.operation_key = self._maintenance_key(
            kind, unit, "" if expected_unit else getattr(self.snapshot, kind + "_invocation", ""))
        dialog.waiting_for_invocation = bool(expected_unit)
        dialog.previous_invocation = (getattr(self.snapshot, kind + "_invocation", "")
                                      if expected_unit == getattr(self.snapshot, kind + "_unit", "") else "")
        dialog.request_error = ""
        dialog.request_completed = False
        dialog.log_text = None
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for name in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + name)(18)
        box.set_margin_top(8)
        dialog.maintenance_summary = label("")
        dialog.maintenance_log_status = label("", "dim-label")
        dialog.maintenance_spinner = Gtk.Spinner()
        dialog.maintenance_spinner.set_size_request(24, 24)
        dialog.maintenance_indicator = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE,
                                                 halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        dialog.maintenance_indicator.add_named(dialog.maintenance_spinner, "running")
        for state, name, css in (("success", "circle-check", "success"),
                                 ("failed", "circle-x", "error"),
                                 ("unknown", "circle-help", "dim-label")):
            status_icon = app_icon(name, 24, STATE_LABELS.get(state, _("Unknown")))
            status_icon.add_css_class(css)
            dialog.maintenance_indicator.add_named(status_icon, state)
        dialog.maintenance_result_row = Gtk.Box(spacing=8)
        dialog.maintenance_result_row.append(dialog.maintenance_indicator)
        dialog.maintenance_summary.set_valign(Gtk.Align.CENTER)
        dialog.maintenance_result_row.append(dialog.maintenance_summary)
        box.append(dialog.maintenance_result_row)
        dialog.maintenance_log_status.set_visible(False)
        box.append(dialog.maintenance_log_status)
        dialog.maintenance_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                              wrap_mode=Gtk.WrapMode.WORD_CHAR)
        dialog.maintenance_scroll = Gtk.ScrolledWindow(min_content_height=220, max_content_height=360,
            propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        dialog.maintenance_scroll.set_child(dialog.maintenance_view)
        box.append(dialog.maintenance_scroll)
        dialog.maintenance_legend = ControllerLegend()
        box.append(dialog.maintenance_legend)
        toolbar.set_content(box)
        set_window_content(dialog, toolbar)
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        def closed(*_):
            self._last_pointer_position.pop(dialog, None)
            if self.modal is dialog:
                self.modal = None
            key = getattr(dialog, "operation_key", ("", "", 0))
            state = getattr(self.snapshot, kind + "_state", "unknown")
            if (key[0] and not dialog.waiting_for_invocation and state != "running"
                    and self._maintenance_key(kind) == key):
                self.maintenance_acknowledged.add((kind, *key))
                self.maintenance_acknowledgement_pending.add((kind, *key))
                client = self.client
                if (not self.preview and client is not None
                        and not getattr(client, "closed", False)):
                    try:
                        client.call("MaintenanceViewed", GLib.Variant("(sssx)", (kind, *key)))
                    except Exception:
                        pass
                self._sync_maintenance_row(kind)
                sync = {"rollback": self._sync_rollback_ui,
                        "cancel": self._sync_cancel_pending_ui,
                        "cache": self._sync_clear_cache_ui,
                        "layer": self._sync_layers_ui,
                        "delete": self._sync_delete_deployments_ui,
                        "channel": self._sync_channel_ui,
                        "image": self._sync_image_ui}.get(kind)
                if sync:
                    sync()
            self._update_controller_legend()
            return False
        dialog.connect("close-request", closed)
        self._sync_maintenance_dialog()
        dialog.present()
        self._update_controller_legend()

    def _maybe_open_maintenance_details(self):
        # Requests open their progress modal immediately. Snapshot changes
        # refresh that modal instead of presenting a second one later.
        return

    @staticmethod
    def _set_maintenance_indicator(dialog, state):
        state = state if state in {"running", "success", "failed"} else "unknown"
        dialog.maintenance_spinner.set_visible(state == "running")
        dialog.maintenance_spinner.start() if state == "running" else dialog.maintenance_spinner.stop()
        dialog.maintenance_indicator.set_visible_child_name(state)

    def _sync_maintenance_dialog(self):
        dialog = self.modal
        if dialog is None or not hasattr(dialog, "maintenance_legend"):
            return
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        dialog.maintenance_legend.set_controller_content(_("Scroll"), [("back", _("Back"), labels)], self._prompt_style(), scrolling=True)
        set_prompt_visible(dialog.maintenance_legend, self._prompts_visible())
        if not hasattr(dialog, "operation_kind"):
            return
        kind = dialog.operation_kind
        key = self._maintenance_key(kind)
        if dialog.waiting_for_invocation:
            state = getattr(self.snapshot, kind + "_state", "unknown")
            if error_text(dialog.request_error, N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog'):
                self.maintenance_pending_units.pop(kind, None)
                self.maintenance_expected_units.pop(kind, None)
                if (key[0] == dialog.operation_key[0] and key[2] > dialog.previous_generation
                        and state in {"failed", "unknown"}):
                    dialog.operation_key = key
                    dialog.waiting_for_invocation = False
                self._set_maintenance_indicator(dialog, "failed")
                dialog.maintenance_summary.remove_css_class("success")
                dialog.maintenance_summary.add_css_class("error")
                dialog.maintenance_summary.set_text(_("Failed · {error}").format(error=format_message(error_text(dialog.request_error, N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog'))))
                return
            if (key[0] == dialog.operation_key[0]
                    and key[2] > dialog.previous_generation
                    and key[1] and key[1] != dialog.previous_invocation):
                dialog.operation_key = key
                dialog.waiting_for_invocation = False
                self.maintenance_pending_units.pop(kind, None)
                self.maintenance_expected_units.pop(kind, None)
            elif (key[0] == dialog.operation_key[0] and key[2] > dialog.previous_generation and not key[1]
                  and state in {"failed", "unknown"}):
                # Backend request records can prove denial/uncertainty without
                # a systemd invocation (for example, authorization failed
                # before the unit started). Preserve that terminal evidence.
                dialog.operation_key = key
                dialog.waiting_for_invocation = False
                self.maintenance_pending_units.pop(kind, None)
                self.maintenance_expected_units.pop(kind, None)
            else:
                dialog.maintenance_summary.set_text({
                    "cache": _("Preparing cache cleanup…"),
                    "cancel": _("Preparing deployment cancellation…"),
                    "channel": _('Preparing release channel rebase…'),
                    "image": _('Preparing image rebase…'),
                    "rollback": _("Preparing rollback…"),
                    "layer": _("Preparing layer removal…"),
                    "delete": _("Preparing deployment deletion…"),
                }.get(kind, _("Preparing operation…")))
                self._set_maintenance_indicator(dialog, "running")
                return
        if key != dialog.operation_key:
            if (key[0] == dialog.operation_key[0] and key[2] == dialog.operation_key[2]
                    and not dialog.operation_key[1] and key[1]):
                # The same request can acquire its journal identity after an
                # earlier status lacked it. This is not a newer operation.
                dialog.operation_key = key
        if key != dialog.operation_key:
            if (key[2] < dialog.operation_key[2]
                    or (key[0] == dialog.operation_key[0] and key[2] == dialog.operation_key[2]
                        and not key[1])):
                # A transient discovery gap is not evidence of another job.
                # Keep the attached invocation and its already-read output.
                dialog.maintenance_log_status.set_text(error_text(getattr(self.snapshot, kind + "_log_error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog') or "")
                dialog.maintenance_log_status.set_visible(bool(dialog.maintenance_log_status.get_text()))
                return
            self._set_maintenance_indicator(dialog, "unknown")
            dialog.maintenance_log_status.set_text(_("A newer operation is available. Reopen this action to view it."))
            dialog.maintenance_log_status.set_visible(True)
            return
        error = error_text(getattr(self.snapshot, kind + "_error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog')
        log_error = error_text(getattr(self.snapshot, kind + "_log_error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog')
        state = getattr(self.snapshot, kind + "_state", "unknown")
        self._set_maintenance_indicator(dialog, state)
        dialog.maintenance_summary.remove_css_class("success")
        dialog.maintenance_summary.remove_css_class("error")
        if state == "success":
            dialog.maintenance_summary.add_css_class("success")
        elif state == "failed":
            dialog.maintenance_summary.add_css_class("error")
        state_label = STATE_LABELS.get(state, _("Unknown"))
        summary = f"{state_label} · {format_message(error)}" if error else state_label
        dialog.maintenance_summary.set_text(summary)
        dialog.maintenance_log_status.set_text(log_error or "")
        dialog.maintenance_log_status.set_visible(bool(log_error))
        logs = getattr(self.snapshot, kind + "_logs", [])
        text = "\n".join(logs)
        if text == dialog.log_text:
            return
        adjustment = dialog.maintenance_scroll.get_vadjustment()
        old_offset = adjustment.get_value()
        follow = dialog.log_text is None or adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - 2
        dialog.maintenance_view.get_buffer().set_text(text)
        dialog.log_text = text
        dialog.log_generation = getattr(dialog, "log_generation", 0) + 1
        generation = dialog.log_generation
        def restore_position():
            if self.modal is dialog and not self.closing and generation == dialog.log_generation:
                current_key = self._maintenance_key(kind)
                if current_key == dialog.operation_key:
                    adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()) if follow else old_offset)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(restore_position)

    def _confirm_remove_layers(self):
        if self._open_maintenance_if_current("layer", "remove"):
            return
        reason = self._layers_reason()
        if reason:
            self.toast(reason)
            return
        target = self.layers_target
        self._show_layer_selection(target, reset_all=False)

    def _confirm_reset_layers(self):
        if self._open_maintenance_if_current("layer", "reset"):
            return
        reason = self._layers_reason()
        if reason:
            self.toast(reason)
            return
        target = self.layers_target
        self.confirm(
            N_("Remove all layered apps?"),
            "This removes all layered apps from the system. It does not remove Bazzite app store (Flatpak apps), Homebrew apps, AppImages, or your personal files. Restart manually when you are ready to apply the change.",
            N_("Remove all layers"),
            lambda: self._request_remove_layers(target["token"]),
        )

    def _show_layer_selection(self, target, reset_all=False):
        reason = self._layers_reason()
        if reason or target is not self.layers_target:
            self.toast(reason or N_("The package list changed. Review it again before confirming."))
            return
        dialog = Adw.Window(transient_for=self, modal=True,
                            title=_("Choose layered packages to remove"), default_width=520, default_height=430)
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        set_window_content(dialog, toolbar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "start", "end"):
            getattr(content, "set_margin_" + edge)(20)
        content.set_margin_top(8)
        content.set_margin_bottom(12)
        toolbar.set_content(content)
        content.append(label(N_("Select the packages to remove. Apps included with Bazzite stay installed.")))
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=180,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        dialog.package_scroll = scroll
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        dialog.package_checks = {}
        for package in target["packages"]:
            check = Gtk.CheckButton()
            package_label = label(package, translate=False)
            package_label.set_margin_end(40)
            check.set_child(package_label)
            check.add_css_class("layer-selection-row")
            check.set_hexpand(True)
            check.set_active(False)
            overlay = Gtk.Overlay()
            overlay.set_child(check)
            prompt = make_button_prompt("confirm", accessible_name=N_("Select"), style=self._prompt_style())
            prompt.set_halign(Gtk.Align.END)
            prompt.set_valign(Gtk.Align.CENTER)
            prompt.set_margin_end(8)
            set_prompt_visible(prompt, False)
            overlay.add_overlay(prompt)
            overlay.set_measure_overlay(prompt, False)
            check.controller_prompt_overlay = overlay
            check.controller_selection_prompt = prompt
            check.controller_prompt_key = (self._prompt_style(), ())
            checks.append(overlay)
            dialog.package_checks[package] = check
        scroll.set_child(checks)
        content.append(scroll)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         halign=Gtk.Align.FILL)
        footer.add_css_class("layer-selection-footer")
        legend_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dialog.layer_legend = legend_box
        set_prompt_visible(legend_box, self._prompts_visible())
        dialog.layer_legend_prompts = []
        legend_items = (("dpad", N_("D-pad Move")), ("back", N_("Back")))
        for index, (command, title) in enumerate(legend_items):
            if index:
                separator = Gtk.Label(label="·")
                separator.add_css_class("dim-label")
                legend_box.append(separator)
            prompt = make_button_prompt(command,
                                        getattr(getattr(self.controller, "backend", None), "button_labels", {}),
                                        title + " · controller button", self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            legend_box.append(prompt)
            title_label = Gtk.Label(label=title)
            title_label.add_css_class("dim-label")
            legend_box.append(title_label)
            dialog.layer_legend_prompts.append((command, title, prompt))
        footer.append(legend_box)
        footer.append(Gtk.Box(hexpand=True))
        review = Gtk.Button(label=_("Remove"))
        review.controller_label = _("Remove")
        review.controller_exclude_focus = True
        review.add_css_class("destructive-action")
        review.set_tooltip_text(_("Review the selected packages before removal"))
        dialog.review_button = review
        review.set_sensitive(False)
        def sync_review_button(*_):
            review.set_sensitive(any(check.get_active() for check in dialog.package_checks.values()))
        for check in dialog.package_checks.values():
            check.connect("toggled", sync_review_button)
        def submit(*_):
            chosen = [name for name, check in dialog.package_checks.items()
                      if check.get_active()]
            if not chosen:
                self.toast(N_("Select at least one layered app to remove."))
                return
            current_reason = self._layers_reason()
            if current_reason or target is not self.layers_target:
                dialog.close()
                self.toast(current_reason or N_("The package list changed. Review it again before confirming."))
                return
            dialog.close()
            names = "\n".join(chosen)
            def confirm_selected():
                confirm_reason = self._layers_reason()
                if confirm_reason or target is not self.layers_target:
                    self.toast(confirm_reason or N_("The package list changed. Review it again before confirming."))
                    return
                self._request_remove_layers(target["token"], chosen)
            self.confirm(
                N_("Remove selected layered packages?"),
                N_("The following packages will be removed:\n") + names + "\n\nRestart manually when you are ready to apply the change.",
                N_("Remove selected packages"),
                confirm_selected,
            )
        review.connect("clicked", lambda *_: submit())
        remove_prompt = make_button_prompt("context_action", accessible_name=N_("Remove · controller button"), style=self._prompt_style())
        set_prompt_visible(remove_prompt, self._prompts_visible())
        footer.append(remove_prompt)
        footer.append(review)
        dialog.fixed_controller_actions = {"context_action": review}
        dialog.fixed_controller_prompts = [("context_action", review, remove_prompt)]
        content.append(footer)
        self.modal = dialog
        dialog.layer_selection = True
        self._install_input_mode_controllers(dialog)
        dialog.connect("close-request", lambda *_: self._clear_confirm_modal(dialog))
        dialog.connect("map", lambda *_: GLib.idle_add(
            lambda: (self._set_controller_focus(next(iter(dialog.package_checks.values())))
                     if self.modal is dialog and self.controller_navigation and dialog.package_checks else None,
                     GLib.SOURCE_REMOVE)[1]))
        dialog.present()
        self._update_controller_legend()

    def _request_remove_layers(self, token, selected=None):
        if self.closing or self.preview:
            return
        reason = self._layers_reason()
        if (reason or self.layers_target["token"] != token
                or (selected is not None and (not selected or any(name not in self.layers_target["packages"] for name in selected)))):
            self.toast(reason or N_("The package list changed. Review it again before confirming."))
            return
        self.layers_request_active = True
        self.layers_feedback = ""
        self.layers_feedback_is_error = False
        self.deployment_summary_fresh = False
        if selected is None:
            expected_unit = f"bazzite-portal-maintenance@reset-layers-{token}.service"
        else:
            indexes = {package: index for index, package in enumerate(self.layers_target["packages"])}
            bitmap = sum(1 << indexes[name] for name in selected)
            expected_unit = f"bazzite-portal-maintenance@remove-selected-layers-{token}-{bitmap:064x}.service"
        self._show_maintenance_log("layer", expected_unit)
        self._sync_layers_ui()
        def finished(error=""):
            if self.closing:
                return
            self.layers_request_active = False
            if error:
                self._clear_maintenance_pending("layer")
            self.layers_feedback = str(error)
            self.layers_feedback_is_error = bool(error)
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "layer":
                self.modal.request_completed = not error
                self.modal.request_error = str(error) if error else ""
                self._sync_maintenance_dialog()
            self.cancel_target_fresh = False
            self._sync_layers_ui()
            self._refresh_system_after_cancel()
        try:
            method = "RemoveSelectedLayers" if selected is not None else "RemoveLayers"
            args = GLib.Variant("(sas)", (token, selected)) if selected is not None else GLib.Variant("(s)", (token,))
            self.client.call(method, args,
                             callback=lambda _: finished(), error_callback=finished)
        except Exception as error:
            finished(str(error))

    def _render_customizations(self, summary):
        while child := self.customization_rows_box.get_first_child():
            self.customization_rows_box.remove(child)
        self.customization_status_row.set_visible(not summary["known"])
        self.customization_status_row.set_title(format_message(error_text(summary["error"], N_('Details are unavailable. Open Logs for more information.'), context='_render_customizations') or N_("Customization details loaded")))
        for entry in summary["rows"]:
            row = Adw.ActionRow(title=entry["title"], subtitle=GLib.markup_escape_text(entry["detail"]))
            row.set_subtitle_lines(0)
            self.customization_rows_box.append(row)

    def _apply_management_snapshot(self, management):
        """Apply one agent-owned deployment revision to every management action."""
        if not isinstance(management, dict):
            return
        revision = management.get("revision", 0)
        if revision == getattr(self, "_management_revision", -1):
            return
        self._management_revision = revision
        known = management.get("known") is True
        self.cancel_target = management.get("cancel_target")
        self.layers_target = management.get("layers_target")
        self.channel_status = management.get("channel_status")
        self.image_status = management.get("image_status")
        self.delete_status = management.get("delete_status")
        self.image_deployment_status = management.get("deployment_status")
        self.cancel_target_fresh = known
        self.channel_status_fresh = known
        self.delete_status_fresh = known
        self.deployment_summary_fresh = known
        self.system_has_snapshot = True
        self.system_loading = False
        self._set_system_refresh_busy(False)
        summary = management.get("summary") or {"known": False, "entries": [],
                                                "error": error_text(management.get("error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_apply_management_snapshot')}
        self._render_deployment_summary(summary)
        self._set_release_identity(summary)
        presentation = system_presentation.summarize_system_presentation(
            {"deployment_status": self.image_deployment_status}, summary)
        for title, value in {"Image": presentation["variant"], "Release channel": presentation["track"]}.items():
            self.system_rows[title].set_subtitle(GLib.markup_escape_text(format_message(value)))
        self._render_customizations({"known": presentation["packages"] != N_("Unknown"),
            "error": error_text(management.get("error"), N_('Details are unavailable. Open Logs for more information.'), context='_apply_management_snapshot') or N_("Layered package information is unavailable."),
            "rows": [{"title": N_("Layered packages"), "detail": presentation["packages"]}]})
        self._sync_image_ui()
        self._sync_delete_deployments_ui()
        self._sync_channel_ui()
        self._sync_layers_ui()
        self._sync_cancel_pending_ui()
        self._sync_rollback_ui()

    def _refresh_system(self):
        if self.closing or self.system_loading:
            return
        if not self.preview:
            if self.client is None or getattr(self.client, "closed", False):
                return
            self.system_loading = True
            self._set_system_refresh_busy(True)
            def failed(error):
                self.system_loading = False
                self._set_system_refresh_busy(False)
                self.toast(public_error(error, N_("Could not refresh system information. Open Logs for details."), context='Refresh system information'))
            try:
                self.client.call(N_("Refresh"), error_callback=failed)
            except Exception as error:
                failed(error)
            return
        self.system_loading = True
        self.deployment_summary_fresh = False
        self.channel_status_fresh = False
        self._sync_rollback_ui()
        self._sync_cancel_pending_ui()
        self._set_system_refresh_busy(True)
        self.deployment_status_row.set_title(format_message(N_("Next restart")))
        self.deployment_status_row.set_subtitle(format_message(N_("Refreshing next-restart information…")))
        self.deployment_status_row.set_sensitive(False)

        def fetch():
            try:
                info = system_information()
                if not isinstance(info, dict):
                    raise ValueError("System information was malformed.")
                summary = deployments.summarize_deployments(info.get("deployment_status"))
                target = cancel_pending.target(info.get("deployment_status"))
                presentation = system_presentation.summarize_system_presentation(info, summary)
                try:
                    channel_status = system.channel_status(info.get("deployment_status"))
                    if not isinstance(channel_status, dict):
                        channel_status = {"available": False, "reason": "Release channel status was malformed.", "state": "unknown"}
                except Exception as channel_error:
                    channel_status = {"available": False,
                                      "reason": str(channel_error) or N_("Release channel status is unavailable."),
                                      "state": "unknown"}
                try:
                    delete_status = system.delete_deployment_status(info.get("deployment_status"))
                except Exception as delete_error:
                    delete_status = {"available": False, "state": "unknown", "reason": str(delete_error), "deployments": []}
                image_status = system.image_switch_status(info.get("deployment_status"))
                return {"info": info, "summary": summary, "cancel_target": target,
                        "presentation": presentation,
                        "layers_target": reset_layers.target(info.get("deployment_status")),
                        "channel_status": channel_status, "delete_status": delete_status, "image_status": image_status, "error": None}
            except Exception as error:
                return {"info": None, "summary": None,
                        "error": str(error) or N_("System information refresh failed.")}

        def loaded(result):
            if self.snapshot.management:
                # An agent revision supersedes any older preview/read-only query.
                self._apply_management_snapshot(self.snapshot.management)
                return
            self.system_loading = False
            self._set_system_refresh_busy(False)
            error = error_text(result.get("error"), N_('Details are unavailable. Open Logs for more information.'), context='loaded') if isinstance(result, dict) else public_error("System information was malformed.", N_("Could not load system information. Open Logs for details."), context="System information")
            if error is not None:
                self.cancel_target_fresh = False
                self.deployment_summary_fresh = False
                self.channel_status_fresh = False
                self.delete_status_fresh = False
                self._sync_delete_deployments_ui()
                self.customization_status_row.set_visible(True)
                self.customization_status_row.set_title(
                    format_message(N_("Refresh failed · showing last loaded customizations") if self.system_has_snapshot
                    else N_("Customization details could not be refreshed")))
                if self.system_has_snapshot:
                    message = _('Showing last loaded details. {error}').format(error=error)
                else:
                    message = str(error)
                self.deployment_status_row.set_title(format_message(N_("Next restart")))
                self.deployment_status_row.set_sensitive(False)
                self.deployment_status_row.set_subtitle(GLib.markup_escape_text(format_message(message)))
                self._sync_cancel_pending_ui()
                self._sync_channel_ui()
                self._sync_layers_ui()
                self._finish_deferred_cancel_refresh()
                return
            info = result["info"]
            current_page = self.stack.get_visible_child_name()
            scroll = self.stack.get_visible_child() if current_page in {N_("System"), N_("Management")} else None
            offset = scroll.get_vadjustment().get_value() if scroll else None
            presentation = result["presentation"]
            for title, value in {"Image": presentation["variant"], "Release channel": presentation["track"]}.items():
                self.system_rows[title].set_subtitle(GLib.markup_escape_text(format_message(value)))
            self._render_deployment_summary(result["summary"])
            self.deployment_summary_fresh = True
            self._set_release_identity(result["summary"])
            self._render_customizations({"known": presentation["packages"] != N_("Unknown"),
                "error": N_("Layered package information is unavailable."),
                "rows": [{"title": N_("Layered packages"), "detail": presentation["packages"]}]})
            self.system_has_snapshot = True
            self.cancel_target = result.get("cancel_target")
            self.cancel_target_fresh = True
            self.channel_status = result.get("channel_status")
            self.image_status = result.get("image_status")
            self.image_deployment_status = info.get("deployment_status")
            self._sync_image_ui()
            self.delete_status = result.get("delete_status")
            self.delete_status_fresh = isinstance(self.delete_status, dict)
            self._sync_delete_deployments_ui()
            self.channel_status_fresh = isinstance(self.channel_status, dict)
            if isinstance(self.channel_status, dict) and self.channel_status.get("state") in {"running", "success", "failed"}:
                self.channel_waiting_status = False
            elif self.channel_waiting_status:
                self.channel_status_polls += 1
                if self.channel_status_polls >= 5:
                    self.channel_waiting_status = False
            self._sync_channel_ui()
            self.layers_target = result.get("layers_target")
            self._sync_layers_ui()
            self._sync_rollback_ui()
            self._sync_cancel_pending_ui()
            self._finish_deferred_cancel_refresh()
            if scroll is not None:
                def restore_reading_position():
                    if not self.closing and self.stack.get_visible_child() is scroll:
                        scroll.get_vadjustment().set_value(offset)
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(16, restore_reading_position)
            self._schedule_channel_poll()
        self.work(fetch, loaded)

    def _delete_deployment_reason(self):
        status = self.delete_status if isinstance(self.delete_status, dict) else {}
        if self.preview:
            return "Deployment deletion is disabled in preview."
        if self.closing or not self.status_received or self.client is None or getattr(self.client, "closed", False):
            return N_("System status is unavailable. Open Logs for details.")
        if self.system_loading or not self.delete_status_fresh:
            return N_("Refresh system information before deleting deployments.")
        if self.delete_request_active or status.get("state") == "running":
            return N_("A deployment is being deleted.")
        if status.get("state") == "unknown":
            return error_text(status.get("error"), N_('Could not check saved Bazzite versions. Open Logs for details.'), context='_delete_deployment_reason') or N_("Deployment deletion status could not be verified.")
        blocker = self._management_blocker_reason("delete")
        if blocker:
            return blocker
        if status.get("available") is not True:
            return error_text(status.get("reason"), N_('Could not check saved Bazzite versions. Open Logs for details.'), context='_delete_deployment_reason') or N_("No saved, pinned deployments can be deleted.")
        return ""

    def _sync_delete_deployments_ui(self):
        reason = self._delete_deployment_reason()
        self.delete_pinned_deployments_row.set_sensitive(not reason)
        self.delete_pinned_deployments_row.set_activatable(not reason)
        state = getattr(self.snapshot, "delete_state", "unavailable")
        if self.delete_request_active or state == "running":
            title = N_("Deleting pinned deployment…")
            subtitle = N_("Open to view operation progress and logs.")
        elif state == "success" and self._maintenance_row_should_open("delete"):
            title, subtitle = N_("Pinned deployment deleted"), error_text(getattr(self.snapshot, "delete_error", ""), N_('Could not check saved Bazzite versions. Open Logs for details.'), context='_sync_delete_deployments_ui')
        elif state == "failed" and self._maintenance_row_should_open("delete"):
            title, subtitle = N_("Deployment deletion failed"), error_text(getattr(self.snapshot, "delete_error", ""), N_('Could not check saved Bazzite versions. Open Logs for details.'), context='_sync_delete_deployments_ui')
        elif state == "unknown" and self._maintenance_row_should_open("delete"):
            title = N_("Deployment deletion status unknown")
            subtitle = error_text(getattr(self.snapshot, "delete_error", ""), N_('Could not check saved Bazzite versions. Open Logs for details.'), context='_sync_delete_deployments_ui') or reason
        else:
            title = N_("Delete pinned deployments")
            subtitle = reason or N_("Choose a saved, pinned system version to delete.")
        self.delete_pinned_deployments_row.set_title(GLib.markup_escape_text(format_message(title)))
        self.delete_pinned_deployments_row.set_subtitle(GLib.markup_escape_text(subtitle))
        self._sync_maintenance_row("delete")

    def _show_deployment_selection(self):
        if self._open_maintenance_if_current("delete"):
            return
        reason = self._delete_deployment_reason()
        if reason:
            self.toast(reason)
            return
        status = self.delete_status
        dialog = Adw.Window(transient_for=self, modal=True, title=_(N_("Delete pinned deployments")),
                            default_width=560, default_height=480)
        dialog.set_application(self.get_application())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, "set_margin_" + edge)(20)
        content.append(label(N_("Choose a pinned system version to delete. Your running system and next restart are protected. Personal files stay.")))
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=180)
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        dialog.deployment_checks = {}
        first = None
        for entry in status.get("deployments", []):
            text = f"{entry['version']} · {entry['image']}\n{entry['identity'][:12]}"
            check = Gtk.CheckButton()
            check.set_child(label(text, translate=False))
            check.update_property([Gtk.AccessibleProperty.LABEL], [text])
            if first is None:
                first = check
            else:
                check.set_group(first)
            choices.append(check)
            dialog.deployment_checks[entry["token"]] = check
        scroll.set_child(choices)
        content.append(scroll)
        dialog.package_scroll = scroll
        dialog.package_checks = dialog.deployment_checks
        dialog.layer_selection = True
        footer = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        cancel = button(N_("Cancel"), dialog.close)
        remove = button(N_("Delete"), lambda: submit())
        remove.add_css_class("destructive-action")
        remove.set_sensitive(False)
        dialog.delete_button = remove
        for check in dialog.deployment_checks.values():
            check.connect("toggled", lambda *_: remove.set_sensitive(any(c.get_active() for c in dialog.deployment_checks.values())))
        def submit():
            chosen = next((token for token, check in dialog.deployment_checks.items() if check.get_active()), None)
            if not chosen:
                return
            reason = self._delete_deployment_reason()
            if reason or status is not self.delete_status:
                dialog.close()
                self.toast(reason or N_("The deployment list changed. Review it again."))
                return
            dialog.close()
            self._request_delete_deployment(chosen)
        prompts = []
        for command, text, control in (("back", N_("Cancel"), cancel), ("context_action", N_("Delete"), remove)):
            control.controller_exclude_focus = True
            prompt = make_button_prompt(command, getattr(getattr(self.controller, "backend", None), "button_labels", {}), text, self._prompt_style())
            set_prompt_visible(prompt, self._prompts_visible())
            choice = Gtk.Box(spacing=6)
            choice.append(prompt)
            choice.append(control)
            footer.append(choice)
            prompts.append((command, control, prompt))
        dialog.fixed_controller_actions = {"back": cancel, "context_action": remove}
        dialog.fixed_controller_prompts = prompts
        content.append(footer)
        toolbar.set_content(content)
        set_window_content(dialog, toolbar)
        self.modal = dialog
        self._install_input_mode_controllers(dialog)
        dialog.connect("close-request", lambda *_: self._clear_confirm_modal(dialog))
        dialog.connect("map", lambda *_: GLib.idle_add(
            lambda: (self._set_controller_focus(first) if self.modal is dialog and self.controller_navigation else None, GLib.SOURCE_REMOVE)[1]))
        dialog.present()
        self._update_controller_legend()

    def _request_delete_deployment(self, token):
        reason = self._delete_deployment_reason()
        if reason or not any(item.get("token") == token for item in (self.delete_status or {}).get("deployments", [])):
            self.toast(reason or N_("The deployment list changed. Review it again."))
            return
        self.delete_request_active = True
        self._sync_delete_deployments_ui()
        self._sync_channel_ui()
        expected_unit = f"bazzite-portal-maintenance@delete-deployment-{token}.service"
        self._show_maintenance_log("delete", expected_unit)
        def finish(error=None):
            self.delete_request_active = False
            if error:
                self._clear_maintenance_pending("delete")
            self.delete_status_fresh = False
            dialog = self.modal
            if dialog is not None and getattr(dialog, "operation_kind", "") == "delete":
                dialog.request_completed = not error
                dialog.request_error = str(error) if error else ""
                self._sync_maintenance_dialog()
            if error:
                self.toast(public_error(error, N_("The request failed. Open Logs for details."), context='System operation request'))
            self._sync_delete_deployments_ui()
            self._refresh_system()
        try:
            self.client.call("DeleteDeployment", GLib.Variant("(s)", (token,)),
                             callback=lambda *_: finish(), error_callback=finish)
        except Exception as error:
            finish(error)

    def _image_switch_reason(self):
        if self.preview:
            return 'Image rebasing is disabled in preview.'
        if self.closing or not self.status_received or self.client is None or getattr(self.client, "closed", False):
            return N_("System status is unavailable. Open Logs for details.")
        blocker = self._management_blocker_reason("image")
        if blocker:
            return blocker
        raw = self.image_deployment_status or {}
        entries = raw.get("deployments", []) if isinstance(raw, dict) else []
        if entries and (entries[0].get("booted") is not True or entries[0].get("staged") is True):
            return N_("Apply or cancel the pending deployment before changing images.")
        status = self.image_status if isinstance(self.image_status, dict) else {}
        if not status.get("available"):
            return error_text(status.get("error"), N_('Could not check Bazzite images. Open Logs for details.'), context='_image_switch_reason') or N_("Changing Bazzite images is unavailable. Open Logs for details.")
        if self.snapshot.image_state == "unknown" or status.get("state") == "unknown":
            return error_text(self.snapshot.image_error, N_('Could not check Bazzite images. Open Logs for details.'), context='_image_switch_reason') or error_text(status.get("error"), N_('Could not check Bazzite images. Open Logs for details.'), context='_image_switch_reason') or N_('Image-rebase status could not be verified.')
        return ""

    def _sync_image_ui(self):
        if not hasattr(self, "channel_specific_row"):
            return
        reason = self._image_switch_reason()
        browse = not reason
        self.channel_specific_row.set_sensitive(browse)
        self.channel_specific_row.set_activatable(browse)
        self.channel_specific_row.set_subtitle(GLib.markup_escape_text(format_message(reason or N_("Choose a Bazzite image, release channel and version."))))
        self.image_details_row.set_visible(False)
        self._sync_maintenance_row("image")

    def _show_image_selection(self):
        if self._open_maintenance_if_current("image"):
            return
        reason = self._image_switch_reason()
        if reason:
            self.toast(reason)
            return
        from bazzite_portal.ui.image_wizard import ImageWizard, format_version

        def query_images():
            try:
                return image_switch.images(system.command, show_unsupported=bool(
                    self.store.preferences()["show_unsupported_rebase_targets"]))
            except Exception as error:
                return {"images": [], "reason": str(error)}

        def query_versions(image, track):
            try:
                return image_switch.versions(image, track, system.command)
            except Exception as error:
                return {"versions": [], "reason": str(error)}

        def selected(image, tag):
            target = image_switch.target(self.image_deployment_status, image, tag)
            reason = self._image_switch_reason() or (error_text(target.get("reason"), N_('Details are unavailable. Open Logs for more information.'), context='selected') if not target.get("available") else "")
            if reason:
                self.toast(reason)
                return
            track = tag.split("-", 1)[0]
            track_label = {"stable": _("Stable"), "testing": _("Testing"), "unstable": _("Unstable")}[track]
            version = _('Latest {track}').format(track=track_label) if tag == track else format_version(tag, track)
            body = (_('Prepare {image}, {version}, for your next restart? Your personal files stay. Administrator authorization is required. Your computer will not restart automatically.').format(image=image, version=version))
            title = N_('Rebase Bazzite image?')
            if target.get("desktop_environment_change") or track == "unstable":
                if not self.store.preferences()["show_unsupported_rebase_targets"]:
                    self.toast(N_("Enable unsupported rebase targets in Settings to choose this image or channel."))
                    return
            self.confirm(title, body, N_('Rebase'), lambda: self._request_image_switch(target))

        ImageWizard(self, query_images=query_images, query_versions=query_versions,
                    on_selected=selected, show_unsupported=bool(
                        self.store.preferences()["show_unsupported_rebase_targets"])).show()

    def _request_image_switch(self, selected):
        reason = self._image_switch_reason()
        target = image_switch.target(self.image_deployment_status, selected["image"], selected["tag"])
        if (target.get("desktop_environment_change") or selected["tag"].split("-", 1)[0] == "unstable") and not self.store.preferences()["show_unsupported_rebase_targets"]:
            self.toast(N_("Enable unsupported rebase targets in Settings to choose this image or channel."))
            return
        if reason or not target.get("available") or target.get("token") != selected["token"]:
            self.toast(reason or error_text(target.get("reason"), N_('Could not check Bazzite images. Open Logs for details.'), context='_request_image_switch') or N_("Deployment information changed. Review it again."))
            return
        self.image_request_active = True
        instance = f"{selected['token']}-{selected['image']}-{selected['tag']}"
        self._sync_image_ui()
        self._show_maintenance_log("image", f"bazzite-portal-maintenance@image-switch-{instance}.service")
        def finish(error=None):
            self.image_request_active = False
            if error:
                self._clear_maintenance_pending("image")
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "image":
                self.modal.request_completed = not error
                self.modal.request_error = str(error) if error else ""
                self._sync_maintenance_dialog()
            self._refresh_system()
        try:
            self.client.call("StartImageSwitch", GLib.Variant("(sss)", (selected["token"], selected["image"], selected["tag"])),
                             callback=lambda *_: finish(), error_callback=finish)
        except Exception as error:
            finish(error)

    def _effective_channel_status(self):
        status = dict(self.channel_status) if isinstance(self.channel_status, dict) else {}
        token = status.get("token", "")
        expected = f"bazzite-portal-maintenance@channel-switch-{token}.service"
        if token and self.snapshot.channel_unit == expected and self.snapshot.channel_state not in {"idle", "unavailable"}:
            status.update(state=self.snapshot.channel_state, unit=self.snapshot.channel_unit,
                          invocation=self.snapshot.channel_invocation, error=self.snapshot.channel_error)
        return status

    def _channel_switch_reason(self):
        status = self._effective_channel_status()
        if self.preview:
            return 'Release channel rebasing is disabled in preview.'
        if self.closing:
            return N_("The application is closing.")
        if not self.status_received or self.client is None or getattr(self.client, "closed", False):
            return N_("System status is unavailable. Open Logs for details.")
        if self.system_loading or not self.channel_status_fresh:
            return N_('Refresh system information before rebasing release channels.')
        if not isinstance(status, dict):
            return N_("Release channel status is unavailable.")
        if self.channel_request_active:
            return N_('A release channel rebase request is already in progress.')
        if status.get("state") == "running":
            return N_('A release channel rebase is already running.')
        if status.get("state") == "unknown":
            return str(error_text(status.get("error"), N_('Could not check the release channel. Open Logs for details.'), context='_channel_switch_reason') or N_("Release channel status could not be verified. Refresh to check again."))
        blocker = self._management_blocker_reason("channel")
        if blocker:
            return blocker
        if status.get("available") is not True:
            return str(error_text(status.get("reason"), N_('Could not check the release channel. Open Logs for details.'), context='_channel_switch_reason') or N_('Release channel rebasing is unavailable.'))
        if status.get("current_track") not in {"stable", "testing", "unstable"}:
            return N_("The current release channel is not verified.")
        if status.get("target_track") not in {"stable", "testing"}:
            return N_("The target release channel is not verified.")
        token = status.get("token")
        if not isinstance(token, str) or not token:
            return public_error('The release channel authorization token is unavailable.', N_("Could not verify this action. Refresh system information and try again."), context="System action verification")
        return ""

    def _sync_channel_ui(self):
        if not hasattr(self, "channel_switch_row"):
            return
        status = self._effective_channel_status()
        current = status.get("current_track")
        target = status.get("target_track")
        state = status.get("state", "unknown")
        reason = self._channel_switch_reason()
        if self.preview:
            title = 'Release channel rebasing disabled in preview'
            detail = "Preview can read system information but cannot change deployments."
        elif state == "running":

            title = _('Rebasing to Bazzite Testing…') if target == "testing" else _('Rebasing to Bazzite Stable…')
            detail = error_text(status.get("error"), N_('Could not check the release channel. Open Logs for details.'), context='_sync_channel_ui') or N_("The image is being prepared. You can keep using the computer.")
        elif self.channel_request_active:
            title, detail = N_('Rebasing release channel…'), ""
        elif state == "success" and self._maintenance_row_should_open("channel"):

            title = _("Bazzite Testing is ready") if target == "testing" else _("Bazzite Stable is ready")
            detail = N_("Restart manually to start the new release channel.")
        elif state == "failed" and self._maintenance_row_should_open("channel"):
            title = N_('Release channel rebase failed')
            detail = error_text(status.get("error"), N_('Could not check the release channel. Open Logs for details.'), context='_sync_channel_ui') or N_("The operation failed. Refresh system information to check the current state.")
        elif reason:

            title = ((_('Rebase to Bazzite Testing') if current == "stable" else _('Rebase to Bazzite Stable'))
                     if current in {"stable", "testing"} else N_('Release channel rebasing unavailable'))
            detail = reason
        else:
            if current == target:

                title = _("Return to the latest Testing release") if target == "testing" else _("Return to the latest Stable release")
            else:
                title = _('Rebase to Bazzite Testing') if target == "testing" else _('Rebase to Bazzite Stable')
            detail = (N_("Download newer changes to try after restarting.")
                      if target == "testing" else N_("Return to the stable release after restarting."))
        self.channel_switch_row.set_title(GLib.markup_escape_text(format_message(title)))
        self.channel_switch_row.set_subtitle(GLib.markup_escape_text(format_message(str(detail))))
        enabled = not reason
        self.channel_switch_row.set_sensitive(enabled)
        self.channel_switch_row.set_activatable(enabled)
        self.channel_details_row.set_visible(False)
        self._sync_maintenance_row("channel")

    def _schedule_channel_poll(self):
        if self.closing or self.preview:
            return
        running = (self.channel_request_active or self.channel_waiting_status or
                   (isinstance(self.channel_status, dict) and self.channel_status.get("state") == "running"))
        if not running or self.channel_poll_source is not None:
            return
        def poll():
            self.channel_poll_source = None
            if not self.closing and not self.system_loading:
                self._refresh_system()
            return GLib.SOURCE_REMOVE
        self.channel_poll_source = GLib.timeout_add(3000, poll)

    def _confirm_channel_switch(self):
        if self._open_maintenance_if_current("channel"):
            return
        reason = self._channel_switch_reason()
        if reason:
            self.toast(reason)
            self._sync_channel_ui()
            return
        status = self.channel_status
        target_name = N_("Bazzite Testing") if status["target_track"] == "testing" else N_("Bazzite Stable")
        version = self._short_deployment_text(status.get("version"), 48)
        image = self._short_deployment_text(status.get("image"), 100)
        action = (_('Return this installation to the latest {target_name} release? ').format(target_name=target_name)
                  if status.get("current_track") == status["target_track"] else
                  _('Rebase this Bazzite installation from {value1} to {target_name}? ').format(value1=status['current_track'], target_name=target_name))
        body = (action +
                _('The running image is {version} ({image}). This downloads and prepares the selected channel. Administrator authorization is required. Your computer will not restart automatically; restart manually after the rebase completes.').format(version=version, image=image))
        token = status["token"]
        title = _('Return to the latest {target_name} release?').format(target_name=target_name) if status.get("current_track") == status["target_track"] else _('Rebase to {target_name}?').format(target_name=target_name)
        self.confirm(title, body, N_('Rebase'),
                     lambda: self._request_channel_switch(token))

    def _request_channel_switch(self, expected_token):
        if self.preview or self.closing:
            return
        reason = self._channel_switch_reason()
        if reason or self.channel_status.get("token") != expected_token:
            self.toast(reason or N_("The release channel status changed. Refresh and confirm again."))
            self._refresh_system()
            return
        self.channel_request_active = True
        expected_unit = f"bazzite-portal-maintenance@channel-switch-{expected_token}.service"
        self._show_maintenance_log("channel", expected_unit)
        self.channel_waiting_status = False
        self.channel_status_polls = 0
        self._sync_channel_ui()
        def accepted(_reply):
            if self.closing:
                return
            self.channel_request_active = False
            self.channel_status_fresh = False
            self.channel_waiting_status = True
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "channel":
                self.modal.request_completed = True
                self._sync_maintenance_dialog()
            self._sync_channel_ui()
            self._schedule_channel_poll()
        def failed(error):
            if self.closing:
                return
            self.channel_request_active = False
            self._clear_maintenance_pending("channel")
            self.channel_status_fresh = False
            self.channel_status = {"state": "failed", "available": False,
                                   "error": str(error) or N_("The release channel request failed.")}
            if self.modal is not None and getattr(self.modal, "operation_kind", "") == "channel":
                self.modal.request_error = str(error)
                self._sync_maintenance_dialog()
            self._sync_channel_ui()
        try:
            self.client.call("StartChannelSwitch", GLib.Variant("(s)", (expected_token,)),
                             callback=accepted, error_callback=failed)
        except Exception as error:
            failed(error)

    def _build_announcements(self):
        page, box = content_page(N_("Community"), N_("Visit Bazzite documentation, community spaces, and source code."))
        resources = group(N_("Resources"))
        self.community_link_rows = {}
        self.community_favicon_images = {}
        # Preview may read existing local artwork, but live cache reads happen
        # alongside downloads on the startup worker below.
        self.community_favicon_paths = favicons.cached_favicons() if self.preview else {}
        self.community_favicon_fetch_started = False
        for link_id, title, description, url in community.LINKS:
            row = Adw.ActionRow(title=GLib.markup_escape_text(_(title)), subtitle=GLib.markup_escape_text(_(description)))
            row.controller_focus_row = True
            row.set_activatable(True)
            row.connect("activated", lambda *_args, target=url: self._open_community_link(target))
            tile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            tile.add_css_class("community-favicon-tile")
            tile.set_size_request(32, 32)
            tile.set_hexpand(False)
            tile.set_vexpand(False)
            tile.set_halign(Gtk.Align.CENTER)
            tile.set_valign(Gtk.Align.CENTER)
            image = Gtk.Image.new_from_icon_name(self._community_fallback_icon(link_id))
            image.set_pixel_size(24)
            image.set_hexpand(True)
            image.set_vexpand(True)
            image.set_halign(Gtk.Align.CENTER)
            image.set_valign(Gtk.Align.CENTER)
            tile.append(image)
            row.add_prefix(tile)
            row.favicon_tile = tile
            row.favicon_image = image
            self.community_favicon_images[link_id] = image
            self._set_community_favicon(link_id, self.community_favicon_paths.get(link_id))
            resources.add(row)
            self.community_link_rows[link_id] = row
        box.append(resources)
        self.stack.add_named(page, N_("Community"))

        announcements_page, announcements_box = content_page(
            N_("Announcements"), N_("Bazzite release and project announcements.")
        )
        announcement_status = Adw.ActionRow(title=_("No announcements available"))
        announcement_status.set_subtitle_lines(2)
        announcement_status.set_visible(False)
        self.announcement_status_row = announcement_status
        announcements_state = group("")
        announcements_state.add(announcement_status)
        announcements_state.set_visible(False)
        self.announcement_status_group = announcements_state
        announcements_box.append(announcements_state)
        self.announcements_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.announcements_group.add_css_class("boxed-list")
        announcements_box.append(self.announcements_group)
        self.announcement_rows = []
        self.stack.add_named(announcements_page, N_("Announcements"))

    def _start_announcements_load(self):
        """Read announcements at startup without blocking page navigation."""

        def fetch():
            try:
                path = announcements.source_path(self.config_path, preview=self.preview)
                return announcements.load_announcements(path), ""
            except announcements.AnnouncementLoadError as error:
                log.warning("Announcements configuration could not be loaded", exc_info=True)
                return None, str(error)
            except Exception:
                log.warning("Announcements configuration could not be loaded", exc_info=True)
                return None, "The announcements file could not be loaded."

        self.work(fetch, self._show_announcements)

    def _show_announcements(self, result):
        items, error = result
        while child := self.announcements_group.get_first_child():
            self.announcements_group.remove(child)
        self.announcement_rows = []
        self.announcements_available = not bool(error)
        home_row = self.home_category_rows["Announcements"]
        home_row.set_sensitive(self.announcements_available)
        home_row.set_activatable(self.announcements_available)
        home_row.set_subtitle(_("View Bazzite release and project announcements.") if self.announcements_available
                              else _("The configuration file for this category is missing or invalid."))
        if error:
            self.announcement_status_group.set_visible(False)
            return
        if not items:
            self.announcement_status_group.set_visible(True)
            self.announcement_status_row.set_title(format_message(N_("No announcements available")))
            self.announcement_status_row.set_subtitle(format_message(""))
            self.announcement_status_row.set_visible(True)
            return
        self.announcement_status_row.set_visible(False)
        self.announcement_status_group.set_visible(False)
        for item in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item["title"]))
            subtitle = item.get("subtitle", "")
            if subtitle:
                row.set_subtitle(GLib.markup_escape_text(subtitle))
            row.set_subtitle_lines(3)
            row.controller_focus_row = True
            row.set_activatable(True)
            row.connect("activated", lambda *_args, target=item["link"]: self._open_community_link(target))
            date_widget = self._announcement_date_widget(item["date"])
            row.add_suffix(date_widget)
            row.announcement_date_widget = date_widget
            self.announcements_group.append(row)
            self.announcement_rows.append(row)

    @staticmethod
    def _announcement_date_widget(value):
        date_parts = announcements.format_date(value, announcements.date.today()).split()
        day = Gtk.Label(label=date_parts[0], valign=Gtk.Align.CENTER)
        day.add_css_class("title-2")
        month = Gtk.Label(label=date_parts[1], valign=Gtk.Align.CENTER)
        month.add_css_class("title-3")
        day_month = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5,
                            halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        day_month.append(day)
        day_month.append(month)
        date_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                           halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        date_box.set_size_request(110, -1)
        date_box.append(day_month)
        if len(date_parts) == 3:
            year = Gtk.Label(label=date_parts[2], halign=Gtk.Align.START)
            year.add_css_class("caption")
            year.add_css_class("dim-label")
            date_box.append(year)
        date_box.add_css_class("announcement-date")
        date_box.day_month_row = day_month
        return date_box

    @staticmethod
    def _community_fallback_icon(link_id):
        return {
            "docs": "help-browser-symbolic",
            "discord": "user-available-symbolic",
            "github": "applications-development-symbolic",
            "announcements": "mail-unread-symbolic",
            "website": "web-browser-symbolic",
            "forums": "user-group-new-symbolic",
            "reddit": "user-available-symbolic",
        }.get(link_id, "web-browser-symbolic")

    def _set_community_favicon(self, link_id, path):
        image = self.community_favicon_images.get(link_id)
        if image is None:
            return
        if path is None:
            image.set_from_icon_name(self._community_fallback_icon(link_id))
            return
        try:
            image.set_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(str(path))))
            self.community_favicon_paths[link_id] = Path(path)
        except (GLib.Error, OSError, TypeError, ValueError):
            image.set_from_icon_name(self._community_fallback_icon(link_id))

    def _ensure_community_favicons(self):
        if self.preview or self.community_favicon_fetch_started:
            return
        self.community_favicon_fetch_started = True
        def cached_loaded(paths):
            self.community_favicon_paths.update(paths)
            for link_id, path in paths.items():
                self._set_community_favicon(link_id, path)
            # Publish valid local artwork before network requests begin. Each
            # download remains bounded by favicons.FETCH_TIMEOUT_SECONDS.
            self.work(lambda: favicons.fetch_favicons(VERSION), fetched_loaded)

        def fetched_loaded(paths):
            self.community_favicon_paths.update(paths)
            for link_id, path in paths.items():
                self._set_community_favicon(link_id, path)

        self.work(favicons.cached_favicons, cached_loaded)

    def _open_community_link(self, url):
        if not community.valid_link(url):
            self.toast(N_("This community link is not a valid HTTPS address."))
            return
        if self.closing:
            return
        if self.preview:
            self.toast("Links are disabled in preview mode.")
            return
        self._launch_community_uri(url)

    def _launch_community_uri(self, url):
        if self.closing:
            return
        if self.preview:
            self.toast("Links are disabled in preview mode.")
            return
        self._run_xdg_open(url, N_("Could not open link"))

    def _build_settings(self):
        page, box = content_page(N_("Settings"))
        box.remove(box.get_first_child())
        preferences = group(N_("Preferences"))
        self.switches = {}
        self.updating_settings = False
        for key, title, subtitle in (
            ("open_at_login", _("Open app at login"), _("Open the window at login. Background monitoring starts independently.")),
            ("show_indicator", _("Hide notification area icon"), _("Hide the update icon in the notification area.")),
            ("show_logs", _("Show console during update"), _("Show the console on the Updates screen. Intended for advanced users.")),
            ("show_disabled_update_steps", _("Show disabled UUPD steps"), _("Display steps disabled in UUPD. This does not enable them.")),
            ("show_unsupported_rebase_targets", _("Show unsupported rebase targets"), _("Include other desktops and the Unstable channel. Intended for advanced testing; these targets may cause problems.")),
            ("skip_one_shot_confirmation", _("Don’t ask before running tools"), _("Skip confirmation for tools that run without an options dialog.")),
            ("always_show_controller_prompts", _("Always show controller prompts"), _("Keep button prompts visible when no controller is connected.")),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.controller_focus_row = True
            switch = Gtk.Switch(active=(not self.store.preferences()[key] if key == "show_indicator" else self.store.preferences()[key]), valign=Gtk.Align.CENTER)
            switch.controller_exclude_focus = True
            switch.set_tooltip_text(format_message(title))
            switch.connect("notify::active", lambda widget, _, selected=key: self._preference(selected, widget.get_active()))
            row.add_suffix(switch)
            row.set_activatable(True)
            row.connect("activated", lambda _row, control=switch: control.set_active(not control.get_active()))
            preferences.add(row)
            self.switches[key] = switch
            if key == "always_show_controller_prompts":
                self.controller_prompts_row = row
        style_values = ["auto", "xbox", "playstation"]
        style_names = [_("Automatic"), "Xbox", "PlayStation"]
        self.controller_style_row = Adw.ActionRow(title=_("Controller prompt style"), subtitle=_("Choose how controller buttons are drawn; controls keep the same actions."))
        self.controller_style_row.controller_focus_row = True
        self.controller_style_dropdown = Gtk.DropDown(model=Gtk.StringList.new(style_names), valign=Gtk.Align.CENTER)
        self.controller_style_dropdown.controller_exclude_focus = True
        self.controller_style_dropdown.set_size_request(-1, 36)
        current_style = self.store.preferences()["controller_prompt_style"]
        self.controller_style_dropdown.set_selected(style_values.index(current_style))
        self.controller_style_dropdown.set_tooltip_text(_("Controller prompt style"))
        self.controller_style_dropdown.connect("notify::selected", self._controller_style_selected)
        self.controller_style_choice = Gtk.Label(label=style_names[style_values.index(current_style)], xalign=0.5,
                                                 hexpand=True, width_chars=max(map(len, style_names)),
                                                 max_width_chars=max(map(len, style_names)))
        self.controller_style_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.controller_style_controls.add_css_class("controller-style-pill")
        self.controller_style_controls.set_size_request(-1, 36)
        self.controller_style_controls.set_valign(Gtk.Align.CENTER)
        self.controller_style_previous = Gtk.Button.new_from_icon_name("pan-start-symbolic")
        self.controller_style_next = Gtk.Button.new_from_icon_name("pan-end-symbolic")
        for arrow in (self.controller_style_previous, self.controller_style_next):
            arrow.set_focusable(False)
            arrow.add_css_class("flat")
            arrow.add_css_class("controller-style-arrow")
        self.controller_style_previous.connect("clicked", lambda *_: self._cycle_controller_style(-1))
        self.controller_style_next.connect("clicked", lambda *_: self._cycle_controller_style(1))
        self.controller_style_controls.append(self.controller_style_previous)
        self.controller_style_controls.append(self.controller_style_choice)
        self.controller_style_controls.append(self.controller_style_next)
        self.controller_style_row.add_suffix(self.controller_style_controls)
        self.controller_style_row.add_suffix(self.controller_style_dropdown)
        self.controller_style_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.controller_style_size_group.add_widget(self.controller_style_controls)
        self.controller_style_size_group.add_widget(self.controller_style_dropdown)
        self.controller_style_row.set_activatable(True)
        self.controller_style_row.connect("activated", self._open_controller_style)
        self.controller_style_dropdown.set_visible(False)
        self._sync_controller_style_mode()
        preferences.add(self.controller_style_row)
        from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
        theme_names = [_(name) for _theme_id, name in THEME_NAMES]
        self.notification_theme_row = Adw.ActionRow(title=_("Notification icon theme"))
        self.notification_theme_row.controller_focus_row = True
        self.notification_theme_row.set_visible(self.preview or NOTIFICATION_THEMES_ENABLED)
        self.notification_theme_dropdown = Gtk.DropDown(model=Gtk.StringList.new(theme_names), valign=Gtk.Align.CENTER)
        self.notification_theme_dropdown.controller_exclude_focus = True
        self.notification_theme_dropdown.set_selected(THEMES.index(self.store.preferences()["notification_icon_theme"]))
        self.notification_theme_dropdown.connect("notify::selected", self._notification_theme_selected)
        self.notification_theme_choice = Gtk.Label(label=theme_names[self.notification_theme_dropdown.get_selected()])
        self.notification_theme_controls = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.notification_theme_controls.add_css_class("controller-style-pill")
        for icon_name, delta in (("pan-start-symbolic", -1), ("pan-end-symbolic", 1)):
            arrow = Gtk.Button.new_from_icon_name(icon_name)
            arrow.set_focusable(False)
            arrow.add_css_class("flat")
            arrow.connect("clicked", lambda _button, step=delta: self._cycle_notification_theme(step))
            self.notification_theme_controls.append(arrow)
            if delta == -1:
                self.notification_theme_controls.append(self.notification_theme_choice)
        self.notification_theme_row.add_suffix(self.notification_theme_controls)
        self.notification_theme_row.add_suffix(self.notification_theme_dropdown)
        self.notification_theme_controls.set_size_request(-1, 36)
        self.notification_theme_dropdown.set_size_request(-1, 36)
        self.notification_theme_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.notification_theme_size_group.add_widget(self.notification_theme_controls)
        self.notification_theme_size_group.add_widget(self.notification_theme_dropdown)
        self.notification_theme_row.set_activatable(True)
        self.notification_theme_row.connect("activated", lambda *_: self.notification_theme_dropdown.emit("activate") if not self.controller_navigation else None)
        preferences.add(self.notification_theme_row)
        self._sync_controller_style_mode()
        box.append(preferences)
        steam_group = group("Steam")
        self.add_to_steam_row = Adw.ActionRow(
            title=_("Add Bazzite Portal to Steam"),
            subtitle=_("Open this app from your Steam library, including Gaming Mode."))
        self.add_to_steam_row.controller_focus_row = True
        self.add_to_steam_row.set_activatable(True)
        self.add_to_steam_row.connect("activated", lambda *_: self._add_to_steam())
        steam_group.add(self.add_to_steam_row)
        box.append(steam_group)
        data = group(N_("Local app data"), str(self.store.root))
        logs_row = Adw.ActionRow(title=_("Open app logs"), subtitle=_("Open the folder containing this app’s private logs."))
        logs_row.controller_focus_row = True
        logs_row.set_activatable(True)
        logs_row.connect("activated", lambda *_: self._open_app_logs_folder())
        data.add(logs_row)
        self.app_logs_row = logs_row
        row = Adw.ActionRow(title=_("Clear local app data"), subtitle=_("Remove pins, preferences, and dismissed reminders. Running jobs are unaffected."))
        row.controller_focus_row = True
        row.set_activatable(True)
        row.connect("activated", lambda *_: self.confirm(N_("Clear local app data?"), N_("Your pins, preferences, and dismissed reminders will be removed. System updates and their logs will not be changed."), N_("Clear data"), self._reset))
        data.add(row)
        box.append(data)
        about_group = group(N_("About"))
        about_row = Adw.ActionRow(title=_("About Bazzite Portal"))
        about_row.controller_focus_row = True
        about_row.set_activatable(True)
        about_row.connect("activated", lambda *_: self.get_application().activate_action("about", None))
        about_group.add(about_row)
        box.append(about_group)
        self.settings_errors = label("\n".join(public_error(error, N_("Could not save or load app preferences. Open Logs for details."), context='App preferences') for error in self.store.errors), translate=False)
        box.append(self.settings_errors)
        self.stack.add_named(page, N_("Settings"))

    def _add_to_steam(self):
        if self.closing:
            return
        if self.preview:
            self.toast("Adding to Steam is disabled in preview mode.")
            return
        if not self.add_to_steam_row.get_sensitive():
            return
        self.add_to_steam_row.set_sensitive(False)
        future = self.executor.submit(steam_shortcut.add_to_steam)

        def finished(result):
            if result.cancelled():
                return
            try:
                result.result()
                message = N_("Add to Steam requested. Check your Steam library.")
            except Exception as error:
                log.warning("Add to Steam failed: %s", error)
                message = public_error(error, N_('Could not add Bazzite Portal to Steam. Open Logs for details.'), context='Add to Steam')

            def present():
                if not self.closing:
                    self.add_to_steam_row.set_sensitive(True)
                    self.toast(message)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(present)

        future.add_done_callback(finished)

    def _open_app_logs_folder(self):
        if self.closing:
            return
        if self.preview:
            self.toast("Folder opening is disabled in preview mode.")
            return
        try:
            from bazzite_portal.core.logging import get_log_directory
            directory = get_log_directory()
        except OSError as error:
            self.toast(public_error(error, N_('Could not open the app logs folder.'), context='Open app logs'))
            return
        self._open_report_folder(directory)

    def _preference(self, key, value):
        if self.updating_settings:
            return
        if key == "show_indicator":
            value = not value
        saved = self.store.set_preference(key, value)
        if not saved:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))
        if key == "show_logs":
            self._set_update_console(value)
        if key == "show_disabled_update_steps":
            self._render_status()
        if key == "notification_icon_theme":
            self._render_home_activity()
        if key in {"always_show_controller_prompts", "controller_prompt_style"}:
            self._refresh_all_prompts()
            set_prompt_visible(self.legend, self._prompts_visible())
        self._reload_agent()
        if saved and key == "show_unsupported_rebase_targets" and value:
            self.confirm(N_("Unsupported rebase targets"),
                         N_("Rebasing to unsupported targets, including other desktops and the Unstable channel, can and will break your system. The Bazzite team reserves the right not to provide support in these situations. Please read our documentation for further information."),
                         N_("OK"), lambda: None, informational=True,
                         documentation_url="https://docs.bazzite.gg/Installing_and_Managing_Software/Updates_Rollbacks_and_Rebasing/rebase_guide/")

    def _controller_style_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        values = ("auto", "xbox", "playstation")
        selected = dropdown.get_selected()
        if selected < len(values):
            self._preference("controller_prompt_style", values[selected])
            self.controller_style_choice.set_text((_("Automatic"), "Xbox", "PlayStation")[selected])

    def _open_controller_style(self, *_):
        if not self.controller_navigation:
            self.controller_style_dropdown.emit("activate")

    def _notification_theme_selected(self, dropdown, _property):
        if self.updating_settings:
            return
        from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
        selected = dropdown.get_selected()
        if selected < len(THEMES):
            self._preference("notification_icon_theme", THEMES[selected])
            self.notification_theme_choice.set_text(THEME_NAMES[selected][1])

    def _cycle_notification_theme(self, delta):
        from bazzite_portal.artwork.appearance import THEMES
        self.notification_theme_dropdown.set_selected((self.notification_theme_dropdown.get_selected() + delta) % len(THEMES))

    def _sync_controller_style_mode(self):
        if not hasattr(self, "controller_style_dropdown"):
            return
        self.controller_style_controls.set_visible(self.controller_navigation)
        self.controller_style_dropdown.set_visible(not self.controller_navigation)
        if hasattr(self, "notification_theme_dropdown"):
            self.notification_theme_controls.set_visible(self.controller_navigation)
            self.notification_theme_dropdown.set_visible(not self.controller_navigation)

    def _cycle_controller_style(self, delta):
        selected = self.controller_style_dropdown.get_selected()
        self.controller_style_dropdown.set_selected((selected + delta) % 3)

    def _reload_agent(self):
        if self.client:
            self.client.call("ReloadPreferences")

    def _reset(self):
        def reset_done(_result=None):
            self.store.errors.clear()
            self.updating_settings = True
            for key, switch in self.switches.items():
                switch.set_active(not self.store.preferences()[key] if key == "show_indicator" else self.store.preferences()[key])
            self.controller_style_dropdown.set_selected(("auto", "xbox", "playstation").index(
                self.store.preferences()["controller_prompt_style"]))
            from bazzite_portal.artwork.appearance import THEMES, THEME_NAMES
            self.notification_theme_dropdown.set_selected(THEMES.index(self.store.preferences()["notification_icon_theme"]))
            self.notification_theme_choice.set_text(_(THEME_NAMES[self.notification_theme_dropdown.get_selected()][1]))
            self.updating_settings = False
            self.controller_style_choice.set_text((_("Automatic"), "Xbox", "PlayStation")[self.controller_style_dropdown.get_selected()])
            self._refresh_all_prompts()
            set_prompt_visible(self.legend, self._prompts_visible())
            self._render_pins()
            self._render_tools()
            self._render_home_activity()
            self._set_update_console(self.store.preferences()["show_logs"])
            self.settings_errors.set_text("\n".join(public_error(error, N_("Could not save or load app preferences. Open Logs for details."), context='App preferences') for error in self.store.errors))
            self.toast(N_("Local app data cleared."))
        if self.client:
            self.client.call("ResetPreferences", callback=reset_done)
        elif self.store.reset():
            reset_done()
        else:
            self.toast(public_error(self.store.errors[-1], N_("Could not save app preferences. Open Logs for details."), context='App preferences'))

    def about(self):
        dialog = Adw.AboutWindow(transient_for=self, application_name=_("Bazzite Portal"), application_icon=APP_ID, version=VERSION, developer_name="Bazzite contributors", license_type=Gtk.License.GPL_3_0_ONLY, comments=_("Manage Bazzite updates, tools, and system settings."))
        translator_credits = _("translator-credits")
        if translator_credits != "translator-credits":
            dialog.set_translator_credits(translator_credits)
        dialog.set_developers(["Xarishark • Zacharias Xenakis https://github.com/Xarishark"])
        dialog.set_website("https://github.com/Xarishark/bazzite-portal")
        dialog.set_issue_url("https://github.com/Xarishark/bazzite-portal/issues")
        dialog.connect("activate-link", self._about_activate_link)
        dialog.present()

    def _about_activate_link(self, _dialog, uri):
        if self.closing:
            return True
        if self.preview:
            self.toast("Links are disabled in preview mode.")
            return True
        self._run_xdg_open(uri, N_("Could not open link"))
        return True

    def open_action_id(self, action_id):
        matches = [item for item in self.items if item["action"].get("id") == action_id]
        self.show_page(N_("Tools"))
        if len(matches) == 1:
            self.open_action(matches[0])
        else:
            self.toast(N_("The requested action is missing or ambiguous."))

    def _controller_context(self):
        window = self.modal or self
        return (id(window), self.stack.get_visible_child_name()) if window.is_active() and window.get_visible() else None

    def _set_controller_focus(self, widget):
        previous = self.controller_focus_widget
        if previous is not None and previous is not widget:
            previous.remove_css_class("bazzite-controller-focus")
            if hasattr(previous, "controller_selection_prompt"):
                set_prompt_visible(previous.controller_selection_prompt, False)
            if hasattr(previous, "favorite_icon"):
                self._set_favorite_emphasis(previous, False)
        self.controller_focus_widget = widget
        self._controller_focus_established = True
        widget.add_css_class("bazzite-controller-immediate")
        widget.add_css_class("bazzite-controller-focus")
        if hasattr(widget, "controller_selection_prompt"):
            set_prompt_visible(widget.controller_selection_prompt, self.controller_navigation and self._prompts_visible())
        if hasattr(widget, "favorite_icon"):
            self._set_favorite_emphasis(widget, True)
        widget.grab_focus()

    def _dispatch(self, command):
        labels = getattr(getattr(self.controller, "backend", None), "button_labels", {})
        self._refresh_context_prompts(labels)
        self._refresh_update_prompts(labels)
        self._refresh_modal_selection_prompts(labels)
        set_prompt_visible(self.legend, self._prompts_visible())
        window = self.modal or self
        wizard = getattr(self.modal, "image_wizard", None)
        if wizard is not None:
            if wizard.dialog.selection_loading:
                return
            if command == "back":
                wizard.back()
                return
            if command == "confirm":
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.Button) and focus.get_sensitive():
                    focus.emit("clicked")
                return
        if self.modal is not None and hasattr(self.modal, "fixed_controller_actions"):
            if getattr(self.modal, "image_selection", False) and command in {"confirm", "left", "right"}:
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.DropDown) and focus.get_sensitive():
                    count = focus.get_model().get_n_items()
                    if count:
                        step = -1 if command == "left" else 1
                        focus.set_selected((focus.get_selected() + step) % count)
                return
            if (command == "confirm" and getattr(self.modal, "layer_selection", False)):
                focus = controller_focused_target(self.modal)
                if isinstance(focus, Gtk.CheckButton):
                    focus.set_active(not focus.get_active())
                return
            control = self.modal.fixed_controller_actions.get(command)
            if command == "back" and control is None and getattr(self.modal, "layer_selection", False):
                self.modal.close()
                return
            if control is not None and control.get_sensitive():
                control.emit("clicked")
            if not ((getattr(self.modal, "layer_selection", False) or getattr(self.modal, "image_selection", False))
                    and command in ("up", "down", "left", "right")):
                return
        if self.modal is not None and hasattr(self.modal, "report_review_scroll"):
            if command in ("up", "down"):
                adjustment = self.modal.report_review_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
                return
        if self.modal is not None and hasattr(self.modal, "maintenance_scroll"):
            if command in ("up", "down", "left", "right"):
                adjustment = self.modal.maintenance_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command in ("up", "left") else 48))
                return
            if command in ("confirm", "focus_search", "context_action", "left", "right"):
                return
        if (self.modal is not None and hasattr(self.modal, "confirm_yes_button")
                and command in ("up", "down", "left", "right")):
            if hasattr(self.modal, "package_scroll") and command in ("up", "down"):
                adjustment = self.modal.package_scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
            return
        if command in ("confirm", "up", "down", "left", "right", "context_action"):
            self.controller_navigation = True
            self._sync_controller_style_mode()
        if self.modal is None and self.stack.get_visible_child_name() == N_("System"):
            if command in ("up", "down"):
                adjustment = self.stack.get_visible_child().get_vadjustment()
                adjustment.set_value(adjustment.get_value() + (-48 if command == "up" else 48))
                self._update_controller_legend(labels)
                return
            if command in ("confirm", "left", "right"):
                return
        if self.modal is None and self.stack.get_visible_child_name() == N_("Updates"):
            self.controller_navigation = True
            self._clear_controller_focus_visuals()
            if command in ("up", "down", "left", "right"):
                if self.update_console_revealer.get_reveal_child():
                    adjustment = self.log_scroll.get_vadjustment()
                    adjustment.set_value(adjustment.get_value() + (-48 if command in ("up", "left") else 48))
            elif command == "confirm":
                self._primary_update_action()
            elif command == "focus_search":
                self._show_release_notes()
            elif command == "back":
                self.show_page(N_("Home"))
            self._update_controller_legend(labels)
            return
        focus_now = controller_focused_target(self.modal or self)
        if (not self.modal and self.stack.get_visible_child_name() == N_("Settings")
                and focus_now is self.notification_theme_row and command in ("left", "right")):
            self._cycle_notification_theme(-1 if command == "left" else 1)
            self._set_controller_focus(self.notification_theme_row)
            self._update_controller_legend(labels)
            return
        if (self.stack.get_visible_child_name() == N_("Settings") and focus_now is self.controller_style_row
                and command in ("left", "right")):
            self._cycle_controller_style(-1 if command == "left" else 1)
            self._set_controller_focus(self.controller_style_row)
            self._update_controller_legend(labels)
            return
        if command == "confirm":
            if self.modal is not None and hasattr(self.modal, "confirm_yes_button"):
                self.modal.confirm_yes_button.emit("clicked")
                self._update_controller_legend(labels)
                return
            focus = controller_focused_target(window)
            if focus is not None and not focus.get_sensitive():
                return
            if (not self.modal and self.stack.get_visible_child_name() == N_("Tools")
                    and self.tools_search_active and focus is self.search):
                self._set_controller_focus(self.search)
                self._open_search_keyboard()
            elif isinstance(focus, Gtk.Switch):
                self._set_controller_focus(focus)
                focus.set_active(not focus.get_active())
            elif isinstance(focus, Gtk.ListBoxRow) and not isinstance(focus, Adw.ActionRow):
                self._set_controller_focus(focus)
                parent = focus.get_parent()
                if isinstance(parent, Gtk.ListBox):
                    parent.select_row(focus)
            elif isinstance(focus, Gtk.Button):
                self._set_controller_focus(focus)
                focus.emit("clicked")
            elif isinstance(focus, Adw.ActionRow) and getattr(focus, "controller_focus_row", False):
                self._set_controller_focus(focus)
                focus.emit("activated")
            elif focus:
                self._set_controller_focus(focus)
                focus.activate()
        elif command in ("up", "down", "left", "right"):
            targets = controller_focus_targets(window)
            if targets:
                focus = controller_focused_target(window)
                if not self._controller_focus_established:
                    target = targets[-1] if command in ("up", "left") else targets[0]
                    self._set_controller_focus(target)
                    GLib.timeout_add(16, self._reveal_focus, window)
                    self._update_controller_legend(labels)
                    return
                try:
                    index = next(index for index, target in enumerate(targets) if target is focus)
                except StopIteration:
                    target = targets[-1] if command in ("up", "left") else targets[0]
                else:
                    step = -1 if command in ("up", "left") else 1
                    if window is self:
                        target = targets[max(0, min(len(targets) - 1, index + step))]
                    else:
                        target = targets[(index + step) % len(targets)]
                self._set_controller_focus(target)
            # Let GTK apply focus and row allocation before measuring bounds.
            # A short one-shot deferral keeps held navigation and scroll in step.
            GLib.timeout_add(16, self._reveal_focus, window)
        elif command == "back":
            if self.modal:
                self.modal.close()
            elif self.stack.get_visible_child_name() == N_("Home"):
                return
            elif self.stack.get_visible_child_name() == N_("Tools") and self.tools_search_active:
                self.close_tools_search()
            else:
                self.show_page(N_("Home"))
        elif command == "context_action" and not self.modal:
            page = self.stack.get_visible_child_name()
            if page == N_("Home"):
                self.controller_navigation = True
                self.show_page(N_("Settings"))
            elif page == N_("Tools"):
                focus = controller_focused_target(self)
                item = getattr(focus, "portal_item", None)
                if item:
                    self._toggle_favorite(item, focus)
        elif command == "focus_search" and not self.modal:
            page = self.stack.get_visible_child_name()
            if page == N_("Home"):
                self.show_page(N_("Updates"))
            elif page in {N_("System"), N_("Management")}:
                self._refresh_system()
            elif page == N_("Tools"):
                self.open_tools_search()
                self._open_search_keyboard()
        self._update_controller_legend(labels)

    def _update_controller_legend(self, labels=None):
        if not hasattr(self, "legend"):
            return
        labels = labels if labels is not None else getattr(getattr(self.controller, "backend", None), "button_labels", {})
        text = _("Move")
        log_open = (self.modal is None and self.stack.get_visible_child_name() == N_("Updates") and
                    self.update_console_revealer.get_reveal_child())
        scrolling = log_open or (self.modal is not None and hasattr(self.modal, "maintenance_scroll"))
        if scrolling:
            text = _("Scroll")
        if (self._prompts_visible() and not self.modal and
                self.stack.get_visible_child_name() == N_("Tools") and not self.tools_search_active):
            target = controller_focused_target(self)
            item = getattr(target, "portal_item", None)
            favorite = bool(item and portal.reference(item) in self.store.pins())
            action = _("Unfavorite") if favorite else _("Favorite")
            has_favorite_action = True
        else:
            has_favorite_action = False
        information_open = self.stack.get_visible_child_name() == N_("System") and not self.modal
        if information_open:
            text = _("Scroll")
            scrolling = True
        prompts = ([] if log_open or information_open else [("confirm", _("Select"), labels)])
        prompts.append(("back", _("Back"), labels))
        if has_favorite_action:
            action = _("Unfavorite") if favorite else _("Favorite")
            prompts.append(("context_action", action, labels))
        if self.stack.get_visible_child_name() == N_("Updates") and not self.modal:
            text = _("D-pad Scroll") if log_open else ""
            scrolling = bool(log_open)
            prompts = [("back", _("Home"), labels)]
        elif self.modal is not None and hasattr(self.modal, "maintenance_scroll"):
            text = _("D-pad Scroll")
            scrolling = True
            prompts = [("back", _("Back"), labels)]
        if self.modal is not None and hasattr(self.modal, "fixed_controller_actions"):
            text = ""
            prompts = [(command, control.get_label(), labels)
                       for command, control in self.modal.fixed_controller_actions.items()]
            if getattr(self.modal, "layer_selection", False):
                text = _("D-pad Move")
                scrolling = False
                prompts = [("confirm", _("Select"), labels), ("back", _("Back"), labels),
                           ("context_action", _("Review selected"), labels)]
            elif getattr(self.modal, "image_selection", False):
                prompts.insert(0, ("back", _("Back"), labels))
        if self.modal is not None and any(
                hasattr(target, "controller_selection_prompt")
                for target in controller_focus_targets(self.modal)):
            prompts = [prompt for prompt in prompts if prompt[0] != "confirm"]
        self.legend.set_controller_content(text, prompts, self._prompt_style(), scrolling=scrolling)

    @staticmethod
    def _reveal_focus(window):
        stack = getattr(window, "stack", None)
        if stack is not None and stack.get_visible_child_name() == N_("Home"):
            page = stack.get_visible_child()
            targets = controller_focus_targets(page)
            if targets and controller_focused_target(window) is targets[0]:
                adjustment = page.get_vadjustment()
                adjustment.set_value(adjustment.get_lower())
                return
        focus = controller_focused_target(window)
        if not focus:
            return
        parent = focus.get_parent()
        while parent:
            if isinstance(parent, Gtk.ScrolledWindow):
                content = parent.get_child()
                if isinstance(content, Gtk.Viewport):
                    content = content.get_child()
                bounds = focus.compute_bounds(content)
                if bounds[0]:
                    rect = bounds[1]
                    adjustment = parent.get_vadjustment()
                    top, bottom = rect.get_y(), rect.get_y() + rect.get_height()
                    lower = adjustment.get_lower()
                    value = adjustment.get_value()
                    size = adjustment.get_page_size()
                    margin = min(12.0, size / 12.0)
                    target_value = value
                    if top < value + margin:
                        target_value = top - margin
                    elif bottom > value + size - margin:
                        target_value = bottom - size + margin
                    upper = adjustment.get_upper() - size
                    adjustment.set_value(max(lower, min(target_value, upper)))
                break
            parent = parent.get_parent()

    def _close(self, *_):
        application = self.get_application()
        if application is not None and application.window is self:
            application.window = None
        self.closing = True
        self.home_update_artwork.close()
        if self._preview_timer is not None:
            GLib.source_remove(self._preview_timer)
            self._preview_timer = None
        self._last_pointer_position.clear()
        if self._native_focus_timeout is not None:
            GLib.source_remove(self._native_focus_timeout)
            self._native_focus_timeout = None
        if self.controller:
            self.controller.close()
        if self._gaming_auth is not None:
            self._gaming_auth.close()
        if self.client:
            self.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.portal_status_executor.shutdown(wait=False, cancel_futures=True)
        return False


class Application(Adw.Application):
    def __init__(self, config_path=portal.DEFAULT_CONFIG, preview=False, updates=False, action_id=None):
        super().__init__(application_id=APP_ID + (".Preview" if preview else ""), flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.config_path, self.preview, self.initial_updates = config_path, preview, updates
        self.window = None
        self.add_main_option("updates", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, N_("Open Updates"), None)
        self.add_main_option("preview", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, "Harmless preview", "YAML")
        self.add_main_option("action-id", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, N_("Open a Portal tool"), "ID")
        self.preview_base = None
        if preview:
            import tempfile
            self.preview_directory = tempfile.TemporaryDirectory(prefix="bazzite-portal-preview-")
            self.preview_base = self.preview_directory.name

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        if gaming_mode():
            self.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        for name, callback in (("settings", lambda: self.activate_page(N_("Settings"))), ("updates", lambda: self.activate_page(N_("Updates"))), ("about", lambda: self.window.about())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _, __, selected=callback: selected())
            self.add_action(action)
        self.set_accels_for_action("app.settings", ["<Primary>comma"])

    def do_activate(self):
        if not self.window:
            self.window = Window(self, self.config_path, self.preview)
            if gaming_mode():
                self.window.fullscreen()
            self.window.connect("destroy", self._window_destroyed)
            window = self.window
            window.home_update_artwork.when_ready(window._refresh_all_prompts)
            window.home_update_artwork.when_ready(
                lambda: window.present() if not window.closing and not window.get_mapped() else None)
        else:
            window = self.window
            window.home_update_artwork.when_ready(lambda: window.present() if not window.closing else None)
        if self.initial_updates:
            self.window.show_page(N_("Updates"))
            self.initial_updates = False

    def _window_destroyed(self, window):
        if self.window is window:
            self.window = None

    def activate_page(self, name):
        self.activate()
        self.window.show_page(name)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        self.activate()
        if options.contains("updates"):
            self.window.show_page(N_("Updates"))
        action_id = options.lookup_value("action-id", GLib.VariantType.new("s"))
        if action_id:
            self.window.open_action_id(action_id.get_string())
        return 0
