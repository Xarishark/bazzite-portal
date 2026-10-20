"""System information and maintenance UI methods for the main window."""
import json

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from bazzite_portal.artwork.icons import icon as app_icon
from bazzite_portal.core.errors import error_text, public_error
from bazzite_portal.core.i18n import C_, N_, _, channel_name, format_message
from bazzite_portal.input.prompts import set_prompt_visible
from bazzite_portal.maintenance import image_switch, rollback_cancel
from bazzite_portal.ui import system_info as system_presentation
from bazzite_portal.ui.widgets import (
    button, content_page, group, label, ControllerLegend,
    make_button_prompt, make_context_prompt, set_window_content,
)
from bazzite_portal.updates import monitor as system

STATE_LABELS = {
    "running": _("Running"), "success": _("Succeeded"), "failed": _("Failed"),
    "unknown": _("Unknown"), "unavailable": _("Unavailable"),
}


class SystemManagementUI:

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
        management_page, management_box = content_page(N_("System Management"), "")
        self.management_box = management_box
        self._add_system_refresh_heading(box, N_("System information"), "system")
        self._add_system_refresh_heading(management_box, N_("System Management"), "management")
        self.system_group = group(C_('{bazzite} Image Version', bazzite='Bazzite'), N_("The image and update track used by your running system."))
        self.system_rows = {}
        for title in (N_("Image"), N_("Release channel")):
            row = Adw.ActionRow(title=format_message(title), subtitle=_("Loading…"))
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

        self.channel_group = group(
            N_("Release channel"),
            _('Follow the latest {stable} or {testing} release of your {bazzite} image.').format(bazzite='Bazzite',
                stable=channel_name("stable"), testing=channel_name("testing")))
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
        self.channel_specific_row = Adw.ActionRow(
            title=_('Rebase to specific image'), subtitle=_('Choose a {bazzite} image, release channel and version.').format(bazzite='Bazzite')
        )
        self.channel_specific_row.controller_focus_row = True
        self.channel_specific_row.add_prefix(app_icon("shield", 20, N_("Requires administrator authorization")))
        self.channel_specific_row.controller_read_only_focus = True
        self.channel_specific_row.set_activatable(True)
        self.channel_specific_row.connect("activated", lambda *_: self._show_image_selection())
        self.channel_group.add(self.channel_specific_row)
        management_box.append(self.channel_group)

        layers_group = group(N_("Remove layered apps"), N_("Remove system layers while keeping your personal files and other apps."))
        self.layers_row = Adw.ActionRow(title=_("Choose layered packages to remove"), subtitle=_("Choose which layered packages to remove. A restart is needed to apply the change."))
        self.layers_row.controller_focus_row = True
        self.layers_row.set_activatable(True)
        self.layers_row.connect("activated", lambda *_: self._confirm_remove_layers())
        layers_group.add(self.layers_row)
        self.reset_layers_row = Adw.ActionRow(
            title=_("Remove all layered apps"),
            subtitle=_('Remove all system layers. Your files, {bazzite} app store apps, {homebrew} apps and {appimages} stay.').format(bazzite='Bazzite', homebrew='Homebrew', appimages='AppImages'),
        )
        self.reset_layers_row.controller_focus_row = True
        self.reset_layers_row.set_activatable(True)
        self.reset_layers_row.connect("activated", lambda *_: self._confirm_reset_layers())
        layers_group.add(self.reset_layers_row)
        management_box.append(layers_group)
        self.layers_target = None
        self.layers_request_active = False
        for row in (self.rollback_row, self.cancel_pending_row, self.clear_cache_row, self.delete_pinned_deployments_row,
                    self.layers_row, self.reset_layers_row):
            shield = app_icon("shield", 20, N_("Requires administrator authorization"))
            shield.set_tooltip_text(format_message(N_("Requires administrator authorization")))
            row.add_prefix(shield)
            row.controller_read_only_focus = True
        self.stack.add_named(page, N_("System"))
        self.stack.add_named(management_page, "Management")

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
        if self.system_loading or not self.deployment_summary_fresh:
            return saved, N_("Checking previous deployment… Refresh system information to check rollback availability.")
        if not summary.get("known"):
            return saved, N_("Previous deployment availability is unknown. Refresh system information.")
        cancellation = rollback_cancel.target(self.image_deployment_status)
        pending_rollback = cancellation.get("available") is True and isinstance(next_restart, dict)
        if pending_rollback:
            saved = {**next_restart, "cancel_token": cancellation["token"]}
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
            unit = (getattr(self, "maintenance_pending_units", {}).get("rollback", {}).get("unit")
                    or getattr(self.snapshot, "rollback_unit", ""))
            cancelling = unit.startswith("bazzite-portal-maintenance@cancel-rollback-")
            if state == "running":
                title = N_("Cancelling rollback…") if cancelling else N_("Rolling back…")
            elif state == "success":
                title = N_("Rollback cancelled") if cancelling else N_("Rollback succeeded")
            elif state == "failed":
                title = N_("Rollback cancellation failed") if cancelling else N_("Rollback failed")
            else:
                title = N_("Rollback status unknown")
            self.rollback_row.set_title(format_message(title))
            self.rollback_row.set_subtitle(GLib.markup_escape_text(format_message(format_message(detail) if detail else _("Open to view operation progress and logs."))))
            self._sync_maintenance_row("rollback")
            return
        saved, reason = self._rollback_eligibility()
        enabled = saved is not None and not reason
        pending_rollback = bool(enabled and saved.get("role") == N_("Next restart"))
        title = (N_("Cancel rollback") if pending_rollback else
                 N_("Roll back to previous deployment") if enabled else
                 N_("Rollback already prepared") if reason.startswith("A rollback is already prepared") else
                 N_('No previous deployment is available.') if reason == N_("No previous deployment is available.") else
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
        self.rollback_row.set_title(format_message(title))
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
        if kind == "rollback" and state == "success":
            if (getattr(self, "deployment_summary_fresh", False)
                    and not getattr(self, "system_loading", False)):
                summary = getattr(self, "deployment_summary", None) or {}
                entries = summary.get("entries", [])
                if (summary.get("known") is True and
                        (rollback_cancel.target(getattr(self, "image_deployment_status", None)).get("available") is True
                         or any(isinstance(entry, dict) and entry.get("role") == N_("Saved deployment")
                                for entry in entries))):
                    return False
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
        can_reopen = bool(allow_open and (request_active or self._maintenance_row_should_open(kind)))
        if can_reopen:
            row.set_sensitive(True)
            row.set_activatable(True)

    def _reconcile_maintenance_pending(self, kind):
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

    def _sync_maintenance_row(self, kind):
        self._reconcile_maintenance_pending(kind)
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

    def _request_rollback(self, cancel_token=None):
        if self.closing:
            return
        if self.client is None or getattr(self.client, "closed", False):
            self.toast(N_("System status is unavailable. Open Logs for details."))
            return
        if cancel_token is not None:
            current, reason = self._rollback_eligibility()
            if reason or current is None or current.get("cancel_token") != cancel_token:
                self.toast(reason or N_("Rollback is unavailable."))
                self._sync_rollback_ui()
                return
        self.rollback_request_active = True
        expected_unit = (f"bazzite-portal-maintenance@cancel-rollback-{cancel_token}.service"
                         if cancel_token else "bazzite-portal-maintenance@rollback.service")
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
            self.deployment_summary_fresh = False
            self._sync_rollback_ui()
            self._refresh_system_after_cancel()
            if error:
                self.toast(public_error(error, N_("Rollback failed. Open Logs for details."), context='Rollback request'))

        try:
            if cancel_token:
                self.client.call("CancelRollback", GLib.Variant("(s)", (cancel_token,)),
                                 callback=lambda *_: finished(), error_callback=finished)
            else:
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
        cancel_token = saved.get("cancel_token")
        cancel_pending_rollback = bool(cancel_token)
        expected_identity = saved.get("identity")
        def start_rollback():
            current, current_reason = self._rollback_eligibility()
            if (current_reason or current is None or current.get("role") != saved.get("role")
                    or current.get("identity") != expected_identity
                    or current.get("cancel_token") != cancel_token):
                self.toast(current_reason or N_("Rollback is unavailable."))
                self._sync_rollback_ui()
                return
            self._request_rollback(cancel_token)
        if cancel_pending_rollback:
            self.confirm(
                N_("Cancel rollback?"),
                _('Keep your current system for the next restart. The saved deployment, version {version}, will remain available.').format(version=version),
                N_("Cancel rollback"), start_rollback,
            )
        else:
            self.confirm(
                C_('Roll back {bazzite}?', bazzite='Bazzite'),
                _('Select the saved deployment, version {version}, for your next boot. A restart is required to use it. Your personal files are preserved.').format(version=version),
                N_("Rollback"), start_rollback,
            )

    def _cancel_pending_eligibility(self):
        target = self.cancel_target if isinstance(self.cancel_target, dict) else None
        state = self.snapshot
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
        state = self.snapshot
        target, reason = self._cancel_pending_eligibility()
        enabled = not reason and target is not None and target.get("available") is True
        actionable = enabled or self._maintenance_row_should_open("cancel")
        self.cancel_pending_row.set_sensitive(actionable)
        self.cancel_pending_row.set_activatable(actionable)
        self.cancel_pending_row.set_subtitle(_(reason))
        if self.cancel_request_active:
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
        absent_pending = (self.cancel_target_fresh and
                         isinstance(target, dict) and target.get("available") is False and
                         "There is no pending deployment" in str(target.get("reason", "")))
        if self.cancel_request_active:
            action_title, action_detail = title, detail
        elif state.cancel_state == "running":
            action_title, action_detail = title, detail
        elif absent_pending:
            # A previous operation result is not a failure to read the current
            # deployment state. Keep unread logs reachable; once acknowledged,
            # return to the authoritative no-pending-deployment status.
            if self._maintenance_row_should_open("cancel"):
                action_title, action_detail = title, N_("Open to view operation progress and logs.")
            else:
                action_title = N_("No pending deployment to cancel")
                action_detail = N_("Your current system will start again. No system change is pending.")
        elif state.cancel_state == "failed" and enabled:
            action_title = N_("Cancel pending deployment")
            action_detail = N_("Cancel only the verified prepared deployment before it is activated.")
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
            _('Cancel the prepared {bazzite} deployment {version} ({image})? This removes only the deployment prepared for the next restart. The running deployment, saved deployment, and personal files remain unchanged. This will not restart the computer.').format(bazzite='Bazzite', version=version, image=image)
        )
        token = target["token"]
        self.confirm(N_("Cancel pending deployment?"), body, N_("Cancel deployment"),
                     lambda: self._request_cancel_pending(token))

    def _request_cancel_pending(self, expected_token):
        if self.closing:
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
            dialog = self._current_maintenance_dialog()
            if dialog is not None and getattr(dialog, "operation_kind", "") == "cancel":
                dialog.request_completed = True
                self._sync_maintenance_dialog()
            self._sync_cancel_pending_ui()
            if self.stack.get_visible_child_name() in {N_("System"), "Management"}:
                self._refresh_system()

        def request_failed(error):
            if self.closing:
                return
            self.cancel_request_active = False
            self.cancel_waiting_snapshot = False
            self._clear_maintenance_pending("cancel")
            self.cancel_feedback = None
            dialog = self._current_maintenance_dialog()
            if dialog is not None and getattr(dialog, "operation_kind", "") == "cancel":
                dialog.request_error = str(error) or N_("The cancellation request failed.")
                self._sync_maintenance_dialog()
            self.cancel_target_fresh = False
            self._sync_cancel_pending_ui()
            if self.stack.get_visible_child_name() in {N_("System"), "Management"}:
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
        elif self.stack.get_visible_child_name() in {N_("System"), "Management"}:
            self.cancel_refresh_after_system_load = False
            self._refresh_system()

    def _finish_deferred_cancel_refresh(self):
        if not self.cancel_refresh_after_system_load:
            return
        self.cancel_refresh_after_system_load = False
        if self.stack.get_visible_child_name() in {N_("System"), "Management"} and not self.closing:
            GLib.idle_add(lambda: (self._refresh_system(), GLib.SOURCE_REMOVE)[1])

    def _layers_reason(self):
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
        busy = state == "running" or self.layers_request_active
        if reason:
            status_subtitle = N_("Open to view operation progress and logs.") if busy else reason
            self.layers_row.set_subtitle(GLib.markup_escape_text(format_message(status_subtitle)))
            self.reset_layers_row.set_subtitle(GLib.markup_escape_text(format_message(status_subtitle)))
        else:
            self.layers_row.set_subtitle(_("Choose which layered packages to remove. A restart is needed to apply the change."))
            self.reset_layers_row.set_subtitle(_('Remove all system layers. Your files, {bazzite} app store apps, {homebrew} apps and {appimages} stay.').format(bazzite='Bazzite', homebrew='Homebrew', appimages='AppImages'))
        self.reset_layers_row.set_sensitive(not reason)
        self.reset_layers_row.set_activatable(not reason)
        self._sync_rollback_ui()
        self._sync_maintenance_row("layer")

    def _cache_clear_reason(self):
        state = self.snapshot
        if state.management and state.management.get("known") is not True:
            return N_("System status is unavailable. Refresh system information before continuing.")
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
        if self.cache_request_active or state.cache_state == "running":
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
        if self.closing:
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
            dialog = self._current_maintenance_dialog()
            if dialog is not None and getattr(dialog, "operation_kind", "") == "cache":
                dialog.request_completed = True
                self._sync_maintenance_dialog()
            self._sync_clear_cache_ui()

        def failed(error):
            if self.closing:
                return
            self.cache_request_active = False
            self._clear_maintenance_pending("cache")
            self.cache_feedback = None
            dialog = self._current_maintenance_dialog()
            if dialog is not None and getattr(dialog, "operation_kind", "") == "cache":
                dialog.request_error = str(error) or N_("The cache cleanup request failed.")
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
        if self.closing or self.modal:
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
            self._render_status()
        dialog = Adw.Window(transient_for=self, modal=True, default_width=560,
                            title={"rollback": (_("Cancel rollback") if unit.startswith("bazzite-portal-maintenance@cancel-rollback-") else _("Rollback")), "cancel": _("Cancel pending deployment"),
                                   "layer": _("Remove layered apps"), "cache": _("Clear download cache"),
                                   "channel": _('Rebase release channel'), "image": _('Rebase {bazzite} image').format(bazzite='Bazzite'),
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
            dialog.maintenance_closed = True
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
                if (client is not None
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

    def _current_maintenance_dialog(self):
        dialog = self.modal
        if dialog is not None and hasattr(dialog, "maintenance_legend"):
            return dialog
        auth_dialog = getattr(self, "_gaming_auth_dialog", None)
        parent = getattr(auth_dialog, "modal_parent", None)
        if (parent is not None and not getattr(parent, "maintenance_closed", False)
                and parent.get_visible() and hasattr(parent, "maintenance_legend")):
            return parent
        return None

    def _sync_maintenance_dialog(self):
        dialog = self._current_maintenance_dialog()
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
                    "rollback": (_("Preparing rollback cancellation…")
                                 if dialog.operation_key[0].startswith("bazzite-portal-maintenance@cancel-rollback-")
                                 else _("Preparing rollback…")),
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
        state = getattr(self.snapshot, kind + "_state", "unknown")
        error = error_text(getattr(self.snapshot, kind + "_error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog',
                           logs=getattr(self.snapshot, kind + "_logs", ()) if state == "failed" else ())
        log_error = error_text(getattr(self.snapshot, kind + "_log_error", ""), N_('Details are unavailable. Open Logs for more information.'), context='_sync_maintenance_dialog')
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
            _('This removes all layered apps from the system. It does not remove {bazzite} app store ({flatpak} apps), {homebrew} apps, {appimages}, or your personal files. Restart manually when you are ready to apply the change.').format(
                bazzite='Bazzite', flatpak='Flatpak', homebrew='Homebrew', appimages='AppImages'),
            N_("Remove all layered apps"),
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
        content.append(label(C_('Select the packages to remove. Apps included with {bazzite} stay installed.', bazzite='Bazzite')))
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
        if self.closing:
            return
        reason = self._layers_reason()
        if (reason or self.layers_target["token"] != token
                or (selected is not None and (not selected or any(name not in self.layers_target["packages"] for name in selected)))):
            self.toast(reason or N_("The package list changed. Review it again before confirming."))
            return
        self.layers_request_active = True
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
        self.customization_status_row.set_title(format_message(error_text(summary["error"], N_('Details are unavailable. Open Logs for more information.'), context='_render_customizations')))
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

    def _delete_deployment_reason(self):
        status = self.delete_status if isinstance(self.delete_status, dict) else {}
        if self.closing or not self.status_received or self.client is None or getattr(self.client, "closed", False):
            return N_("System status is unavailable. Open Logs for details.")
        if self.system_loading or not self.delete_status_fresh:
            return N_("Refresh system information before deleting deployments.")
        if self.delete_request_active or status.get("state") == "running":
            return N_("A deployment is being deleted.")
        if status.get("state") == "unknown":
            return error_text(status.get("error"), C_('Could not check saved {bazzite} versions. Open Logs for details.', bazzite='Bazzite'), context='_delete_deployment_reason') or N_("Deployment deletion status could not be verified.")
        blocker = self._management_blocker_reason("delete")
        if blocker:
            return blocker
        if status.get("available") is not True:
            return error_text(status.get("reason"), C_('Could not check saved {bazzite} versions. Open Logs for details.', bazzite='Bazzite'), context='_delete_deployment_reason') or N_("No saved, pinned deployments can be deleted.")
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
            title, subtitle = N_("Pinned deployment deleted"), error_text(getattr(self.snapshot, "delete_error", ""), C_('Could not check saved {bazzite} versions. Open Logs for details.', bazzite='Bazzite'), context='_sync_delete_deployments_ui')
        elif state == "failed" and self._maintenance_row_should_open("delete"):
            title, subtitle = N_("Deployment deletion failed"), error_text(getattr(self.snapshot, "delete_error", ""), C_('Could not check saved {bazzite} versions. Open Logs for details.', bazzite='Bazzite'), context='_sync_delete_deployments_ui')
        elif state == "unknown" and self._maintenance_row_should_open("delete"):
            title = N_("Deployment deletion status unknown")
            subtitle = error_text(getattr(self.snapshot, "delete_error", ""), C_('Could not check saved {bazzite} versions. Open Logs for details.', bazzite='Bazzite'), context='_sync_delete_deployments_ui') or reason
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
            return error_text(status.get("error"), C_('Could not check {bazzite} images. Open Logs for details.', bazzite='Bazzite'), context='_image_switch_reason') or C_('Changing {bazzite} images is unavailable. Open Logs for details.', bazzite='Bazzite')
        if self.snapshot.image_state == "unknown" or status.get("state") == "unknown":
            return error_text(self.snapshot.image_error, C_('Could not check {bazzite} images. Open Logs for details.', bazzite='Bazzite'), context='_image_switch_reason') or error_text(status.get("error"), C_('Could not check {bazzite} images. Open Logs for details.', bazzite='Bazzite'), context='_image_switch_reason') or N_('Image-rebase status could not be verified.')
        return ""

    def _sync_image_ui(self):
        if not hasattr(self, "channel_specific_row"):
            return
        reason = self._image_switch_reason()
        browse = not reason
        self.channel_specific_row.set_sensitive(browse)
        self.channel_specific_row.set_activatable(browse)
        self.channel_specific_row.set_subtitle(GLib.markup_escape_text(format_message(reason or C_('Choose a {bazzite} image, release channel and version.', bazzite='Bazzite'))))
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
            track_label = channel_name(track)
            version = _('Latest {track}').format(track=track_label) if tag == track else format_version(tag, track)
            body = (_('Prepare {image}, {version}, for your next restart? Your personal files stay. Administrator authorization is required. Your computer will not restart automatically.').format(image=image, version=version))
            title = C_('Rebase {bazzite} image?', bazzite='Bazzite')
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
            self.toast(reason or error_text(target.get("reason"), C_('Could not check {bazzite} images. Open Logs for details.', bazzite='Bazzite'), context='_request_image_switch') or N_("Deployment information changed. Review it again."))
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
        if state == "running":

            track = "testing" if target == "testing" else "stable"
            title = _('Rebasing to {bazzite} {track}…').format(bazzite='Bazzite', track=channel_name(track))
            detail = error_text(status.get("error"), N_('Could not check the release channel. Open Logs for details.'), context='_sync_channel_ui') or N_("The image is being prepared. You can keep using the computer.")
        elif self.channel_request_active:
            title, detail = N_('Rebasing release channel…'), ""
        elif state == "success" and self._maintenance_row_should_open("channel"):

            track = "testing" if target == "testing" else "stable"
            title = _('{bazzite} {track} is ready').format(bazzite='Bazzite', track=channel_name(track))
            detail = N_("Restart manually to start the new release channel.")
        elif state == "failed" and self._maintenance_row_should_open("channel"):
            title = N_('Release channel rebase failed')
            detail = error_text(status.get("error"), N_('Could not check the release channel. Open Logs for details.'), context='_sync_channel_ui',
                                logs=self.snapshot.channel_logs) or N_("The operation failed. Refresh system information to check the current state.")
        elif reason:

            title = (_('Rebase to {bazzite} {track}').format(bazzite='Bazzite',
                         track=channel_name("testing" if current == "stable" else "stable"))
                     if current in {"stable", "testing"} else N_('Release channel rebasing is unavailable.'))
            detail = reason
        else:
            if current == target:

                title = _('Return to the latest {track} release').format(track=channel_name(target))
            else:
                title = _('Rebase to {bazzite} {track}').format(bazzite='Bazzite', track=channel_name(target))
            detail = (N_("Download newer changes to try after restarting.")
                      if target == "testing" else
                      _('Return to the {track} release after restarting.').format(
                          track=channel_name(target)))
        self.channel_switch_row.set_title(GLib.markup_escape_text(format_message(title)))
        self.channel_switch_row.set_subtitle(GLib.markup_escape_text(format_message(str(detail))))
        enabled = not reason
        self.channel_switch_row.set_sensitive(enabled)
        self.channel_switch_row.set_activatable(enabled)
        self._sync_maintenance_row("channel")

    def _schedule_channel_poll(self):
        if self.closing:
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
        target_name = f'Bazzite {channel_name(status["target_track"])}'
        version = self._short_deployment_text(status.get("version"), 48)
        image = self._short_deployment_text(status.get("image"), 100)
        action = (_('Return this installation to the latest {target_name} release? ').format(target_name=target_name)
                  if status.get("current_track") == status["target_track"] else
                _('Rebase this {bazzite} installation from {current_track} to {target_name}? ').format(bazzite='Bazzite',
                    current_track=channel_name(status['current_track']), target_name=target_name))
        body = (action +
                _('The running image is {version} ({image}). This downloads and prepares the selected channel. Administrator authorization is required. Your computer will not restart automatically; restart manually after the rebase completes.').format(version=version, image=image))
        token = status["token"]
        title = _('Return to the latest {target_name} release?').format(target_name=target_name) if status.get("current_track") == status["target_track"] else _('Rebase to {target_name}?').format(target_name=target_name)
        self.confirm(title, body, N_('Rebase'),
                     lambda: self._request_channel_switch(token))

    def _request_channel_switch(self, expected_token):
        if self.closing:
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
