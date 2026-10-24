"""Guided GTK frontend for selecting a specific Bazzite image and version."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable

from bazzite_portal.core.i18n import C_, _, N_, channel_name
from bazzite_portal.core.errors import public_error


_DATED_TAG = re.compile(r"^(?:stable|testing|unstable)-(?P<major>\d{2,3})\.(?P<date>\d{8})(?:\.(?P<patch>\d+))?(?:\.(?P<hotpatch>\d+))?$")
_PICKER_STYLE = None


def _install_picker_style(ui):
    global _PICKER_STYLE
    if _PICKER_STYLE is not None:
        return
    _PICKER_STYLE = ui.Gtk.CssProvider()
    _PICKER_STYLE.load_from_data(
        b"window.brh-image-picker button.brh-image-picker-option { padding: 4px 8px; min-height: 0; }"
        b"window.brh-image-picker spinner.brh-image-picker-spinner:checked { animation-duration: 2s; }"
    )
    display = ui.Gdk.Display.get_default()
    if display is not None:
        ui.Gtk.StyleContext.add_provider_for_display(
            display, _PICKER_STYLE, ui.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)


def selectable_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return well-formed choices from the bundled Bazzite image catalog."""
    return [entry for entry in images
            if isinstance(entry, dict) and isinstance(entry.get("image"), str)]


def track_subtitle(track: str) -> str:
    if track == "unstable":
        return _("Experimental builds for development and testing; may be incomplete or broken")
    return (_("Recommended releases for everyday use") if track == "stable"
            else _("Preview upcoming changes; may be less stable"))


def _track_label(track: str) -> str:

    return channel_name(track)


def format_version(tag: str, track: str) -> str:
    """Format dated release tags, retaining the raw tag for unknown forms."""
    match = _DATED_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None or track not in {"stable", "testing", "unstable"} or not tag.startswith(track + "-"):
        return tag if isinstance(tag, str) else str(tag)
    try:
        date = datetime.strptime(match.group("date"), "%Y%m%d").strftime("%d-%m-%Y")
    except ValueError:
        return tag
    track_label = _track_label(track)
    label = f"Bazzite {track_label} {match.group('major')} - {date}"
    patch = match.group("patch")
    if patch is not None:
        label += " " + _("Patch {number}").format(number=patch)
    hotpatch = match.group("hotpatch")
    if hotpatch is not None:
        label += f".{hotpatch}"
    return label


def dated_track_tags(tags: list[str], track: str) -> list[str]:
    """Keep only dated releases for a requested channel, newest first."""
    if track not in {"stable", "testing", "unstable"}:
        return []
    selected = []
    for tag in set(tags):
        match = _DATED_TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match is None or not tag.startswith(track + "-"):
            continue
        try:
            datetime.strptime(match.group("date"), "%Y%m%d")
        except ValueError:
            continue
        selected.append(tag)
    return sorted(selected, key=lambda tag: tuple(int(part) for part in re.findall(r"\d+", tag)), reverse=True)


def available_tracks(image: str, *, show_unsupported: bool = False) -> list[str]:
    """Use the known image-family channels without a registry lookup."""
    if image == "bazzite-dx" or image.startswith("bazzite-dx-"):
        return ["stable"]
    return ["stable", "testing", "unstable"] if show_unsupported else ["stable", "testing"]


class ImageWizard:
    """A three-stage image picker. Query callbacks run in the owner's worker pool."""

    def __init__(self, owner, *, query_images: Callable[[], dict[str, Any]],
                 query_versions: Callable[[str, str], dict[str, Any]],
                 on_selected: Callable[[str, str], None], show_unsupported: bool = False,
                 restore_target=None):
        self.owner = owner
        self.query_images = query_images
        self.query_versions = query_versions
        self.on_selected = on_selected
        self.show_unsupported = show_unsupported
        self.restore_target = restore_target
        self._generation = 0
        self._stage = "images"
        self._image = ""
        self._track = ""
        self._tags: list[str] = []
        self._tracks: list[str] = []
        self._version_results: dict[tuple[str, str], dict[str, Any]] = {}
        # Import at construction time to avoid a module import cycle with ui.py.
        from bazzite_portal.ui import application as ui
        self.ui = ui

    def show(self):
        ui, owner = self.ui, self.owner
        page = owner.stack.get_visible_child()
        adjustment = page.get_vadjustment() if hasattr(page, "get_vadjustment") else None
        self._restore = (self.restore_target or owner.get_focus(),
                         owner.stack.get_visible_child_name(), adjustment,
                         adjustment.get_value() if adjustment is not None else None,
                         owner.controller_navigation)
        self.dialog = ui.Adw.Window(transient_for=owner, modal=True,
                                    title=_("Choose an Image"), default_width=560,
                                    default_height=520)
        self.dialog.set_application(owner.get_application())
        self.dialog.add_css_class("brh-image-picker")
        _install_picker_style(ui)
        self.dialog.image_selection = True
        self.dialog.image_wizard = self
        self.dialog.selection_loading = False
        self.dialog.fixed_controller_actions = {}
        toolbar = ui.Adw.ToolbarView()
        header = ui.Adw.HeaderBar()
        toolbar.add_top_bar(header)
        page = ui.Gtk.Box(orientation=ui.Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "bottom", "start", "end"):
            getattr(page, "set_margin_" + edge)(20)
        page.set_margin_top(8)
        page.set_margin_bottom(12)
        self.status = ui.label("", "dim-label")
        self.status.set_visible(False)
        page.append(self.status)
        self.spinner = ui.Gtk.Spinner()
        self.spinner.add_css_class("brh-image-picker-spinner")
        self.spinner.set_size_request(48, 48)
        self.spinner.set_halign(ui.Gtk.Align.CENTER)
        self.spinner.set_valign(ui.Gtk.Align.CENTER)
        self.loading_panel = ui.Gtk.Box(orientation=ui.Gtk.Orientation.VERTICAL,
                                        spacing=12, valign=ui.Gtk.Align.CENTER,
                                        halign=ui.Gtk.Align.CENTER, vexpand=True)
        self.loading_message = ui.Gtk.Label(wrap=True, justify=ui.Gtk.Justification.CENTER)
        self.loading_message.add_css_class("dim-label")
        self.loading_panel.append(self.spinner)
        self.loading_panel.append(self.loading_message)
        page.append(self.loading_panel)
        self.list_box = ui.Gtk.ListBox(selection_mode=ui.Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll = ui.Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroll = scroll
        scroll.set_policy(ui.Gtk.PolicyType.NEVER, ui.Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.list_box)
        page.append(scroll)
        self.back_button = ui.Gtk.Button(icon_name="go-previous-symbolic")
        self.back_button.set_tooltip_text(_("Back"))
        self.back_button.update_property([ui.Gtk.AccessibleProperty.LABEL], [_("Back")])
        self.back_button.connect("clicked", lambda *_: self.back())
        back_group = ui.Gtk.Box(spacing=6)
        back_group.append(self.back_button)
        self.back_prompt = ui.make_button_prompt("back", accessible_name=_("Back"), style=owner._prompt_style())
        ui.set_prompt_visible(self.back_prompt, owner._prompts_visible())
        back_group.append(self.back_prompt)
        header.pack_start(back_group)
        self.cancel_button = ui.button(_("Cancel"), self.cancel)
        self.back_button.controller_exclude_focus = True
        self.cancel_button.controller_exclude_focus = True
        footer = ui.Gtk.Box(spacing=12)
        self.dialog.picker_legend = ui.ControllerLegend()
        footer.append(self.dialog.picker_legend)
        footer.append(ui.Gtk.Box(hexpand=True))
        footer.append(self.cancel_button)
        page.append(footer)
        toolbar.set_content(page)
        ui.set_window_content(self.dialog, toolbar)
        owner.modal = self.dialog
        owner._install_input_mode_controllers(self.dialog)
        self.dialog.connect("close-request", self._closed)
        self.dialog.present()
        owner._update_controller_legend()
        self._load_images()
        return self.dialog

    def _closed(self, dialog):
        self._generation += 1
        current = self.owner.modal is dialog
        self.owner._clear_confirm_modal(dialog)
        if current:
            self.restore_after_close(dialog)
        return False

    def restore_after_close(self, dialog):
        """Return to the originating row unless another surface replaced it."""
        app = self.owner.get_application()
        activate = dialog.is_active() or (app is not None and app.get_active_window() is dialog)
        def restore():
            owner = self.owner
            target, page_name, adjustment, value, controller = self._restore
            if (owner.closing or owner.modal is not None or target is None
                    or owner.stack.get_visible_child_name() != page_name
                    or not target.get_mapped() or not target.get_sensitive()):
                return self.ui.GLib.SOURCE_REMOVE
            if activate:
                owner.present()
            if adjustment is not None:
                adjustment.set_value(value)
            if controller and owner.controller_navigation:
                owner._set_controller_focus(target)
            else:
                owner._native_input_mode()
                target.grab_focus()
            owner._update_controller_legend()
            return self.ui.GLib.SOURCE_REMOVE
        self.ui.GLib.idle_add(restore)

    def _clear_rows(self):
        while (row := self.list_box.get_row_at_index(0)) is not None:
            self.list_box.remove(row)

    def _row(self, title: str, callback: Callable[[], None], *, subtitle: str = ""):
        ui = self.ui
        button = ui.ModalOptionButton()
        button.add_css_class("brh-image-picker-option")
        button.controller_label = title
        text = ui.Gtk.Box(orientation=ui.Gtk.Orientation.VERTICAL, spacing=4,
                          margin_end=42, margin_top=5, margin_bottom=5)
        text.append(ui.Gtk.Label(label=title, xalign=0, hexpand=True, wrap=True))
        if subtitle:
            text.append(ui.label(subtitle, "dim-label"))
        button.set_child(text)
        button.update_property([ui.Gtk.AccessibleProperty.LABEL], [title])
        button.controller_focus_row = True
        button.set_hexpand(True)
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: callback())
        overlay = ui.Gtk.Overlay()
        child = button.get_child()
        button.set_child(None)
        overlay.set_child(child)
        button.set_child(overlay)
        prompt = ui.make_button_prompt("confirm", accessible_name=_("Select"), style=self.owner._prompt_style())
        prompt.set_halign(ui.Gtk.Align.END)
        prompt.set_valign(ui.Gtk.Align.CENTER)
        prompt.set_margin_end(8)
        ui.set_prompt_visible(prompt, False)
        overlay.add_overlay(prompt)
        overlay.set_measure_overlay(prompt, False)
        button.controller_prompt_overlay = overlay
        button.controller_selection_prompt = prompt
        self.list_box.append(button)
        button.get_parent().controller_skip_self = True
        return button, button

    def _state(self, stage: str, heading: str, status: str = "", loading: bool = False):
        self._stage = stage
        track_label = _track_label(self._track or "stable")
        self.dialog.set_title({"images": _("Choose an Image"), "tracks": _("Choose Channel"),
                               "versions": _("Choose a {track} Version").format(
                                   track=track_label)}.get(stage, heading))
        self.status.set_text(status)
        self.status.set_visible(bool(status) and not loading)
        self.dialog.selection_loading = loading
        self.spinner.set_visible(loading)
        self.loading_panel.set_visible(loading)
        self.loading_message.set_text(status or heading)
        self.scroll.set_visible(not loading)
        if loading:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.back_button.set_sensitive(not loading)
        self.cancel_button.set_sensitive(not loading)
        self._clear_rows()
        self.owner._refresh_all_prompts()

    def _run(self, query, done):
        self._generation += 1
        generation = self._generation
        self.dialog.selection_loading = True
        def loaded(result):
            if self.owner.modal is not self.dialog or generation != self._generation:
                return
            self.dialog.selection_loading = False
            done(result)
            self._focus_choices()
        self.owner.work(query, loaded)

    def _focus_choices(self):
        def focus():
            if self.owner.modal is self.dialog and not self.dialog.selection_loading:
                targets = self.ui.controller_focus_targets(self.dialog)
                if targets:
                    if self.owner.controller_navigation:
                        self.owner._set_controller_focus(targets[0])
                    else:
                        targets[0].grab_focus()
                self.owner._update_controller_legend()
            return self.ui.GLib.SOURCE_REMOVE
        self.ui.GLib.idle_add(focus)

    def _load_images(self):
        self._state("images", _("Loading available images…"), _("Please wait."), True)
        def done(result):
            images = selectable_images(result.get("images", []))
            self._images = images
            reason = public_error(result.get("reason"), C_('Could not load available {bazzite} images. Try again.', bazzite='Bazzite'),
                                  context="Loading Bazzite images") if result.get("reason") else ""
            self._state("images", _("Choose an Image"), reason)
            if not images:
                self.status.set_text(reason or _("No images are available."))
                self.status.set_visible(True)
                return
            for entry in images:
                image = entry["image"]
                self._row(image, lambda selected=image: self._choose_image(selected))
        self._run(self.query_images, done)

    def _choose_image(self, image: str):
        self._generation += 1
        if image != self._image:
            self._version_results.clear()
        self._image = image
        self._track = ""
        self._tracks = available_tracks(image, show_unsupported=self.show_unsupported)
        self._show_tracks()

    def _show_tracks(self):
        if len(self._tracks) == 1:
            self._choose_track(self._tracks[0])
            return
        self._state("tracks", _("Choose Channel"))
        for track in self._tracks:
            self._row(_track_label(track), lambda selected=track: self._choose_track(selected),
                      subtitle=track_subtitle(track))
        self._focus_choices()

    def _choose_track(self, track: str):
        if track not in self._tracks:
            return
        self._generation += 1
        self._track = track
        cached = self._version_results.get((self._image, track))
        if cached is not None:
            self._show_versions(cached)
            self._focus_choices()
            return
        self._state("versions", _("Loading versions…"), _("Please wait."), True)
        image = self._image
        self._run(lambda: self.query_versions(image, track), self._show_versions)

    def _show_versions(self, result):
        if not isinstance(result, dict):
            result = {"reason": "The registry returned malformed version data."}
        versions = result.get("versions", [])
        if not isinstance(versions, list) or any(not isinstance(tag, str) for tag in versions):
            result = {"reason": "The registry returned a malformed version list."}
            versions = []
        if result.get("reason"):
            versions = []
        track = self._track
        tags = dated_track_tags(versions, track)
        self._tags = ([track] if track in versions else []) + tags
        if self._tags and not result.get("reason"):
            self._version_results[(self._image, track)] = result
        track_label = _track_label(track)
        reason = public_error(result.get("reason"), C_('Could not load {bazzite} versions. Try again.', bazzite='Bazzite'),
                              context="Loading Bazzite versions") if result.get("reason") else ""
        self._state("versions", _("Choose a {track} Version").format(track=track_label), reason)
        for tag in self._tags:
            title = _("Latest {track}").format(track=track_label) if tag == track else format_version(tag, track)
            self._row(title, lambda selected=tag: self._select_version(selected))
        if not self._tags:
            self.status.set_text(reason or _("No dated releases are available."))
            self.status.set_visible(True)

    def _select_version(self, tag: str):
        self.dialog.close()
        self.on_selected(self._image, tag)

    def back(self):
        if self.dialog.selection_loading:
            return
        self._generation += 1
        if self._stage == "versions":
            if len(self._tracks) == 1:
                self._load_images()
            else:
                self._show_tracks()
        elif self._stage == "tracks":
            self._load_images()
        else:
            self.cancel()

    def cancel(self):
        self._generation += 1
        self.dialog.close()
