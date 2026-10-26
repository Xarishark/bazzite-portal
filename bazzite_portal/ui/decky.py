"""Decky settings and the acknowledged, event-driven desktop installation offer."""
import json
import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib
from bazzite_portal.core.i18n import C_, N_, _, format_message
from bazzite_portal.core.errors import public_error

log = logging.getLogger(__name__)












def add_row(owner, group):
    row = Adw.ActionRow(title=_("Install Bazzite Portal Decky plugin"),
                        subtitle=_("Use Bazzite Portal from Decky Loader in Gaming Mode."))
    row.controller_focus_row = True
    row.set_sensitive(False)
    row.connect("activated", lambda *_: install(owner))
    owner.decky_row = row
    owner.decky_status = {}
    owner.decky_request_pending = False
    group.add(row)


def render(owner):
    status = owner.decky_status
    busy = owner.decky_request_pending or status.get("installing")
    allowed = bool(status.get("can_install") or status.get("can_update") or status.get("can_downgrade")) and not busy
    if busy:
        title, description = N_("Checking Decky plugin…"), N_("Waiting for Decky Loader…")
    elif status.get("prompt_requested"):
        title, description = N_("Confirm in Decky Loader"), N_("Confirm the installation in Decky Loader, then return to Settings to refresh its status.")
        allowed = False
    elif status.get("can_update"):
        title = N_("Update Bazzite Portal Decky plugin")
        description = C_("Update the installed plugin from {installed} to {bundled}. Decky Loader preserves its settings.",
                         installed=status.get("installed_version", ""), bundled=status.get("bundled_version", ""))
    elif status.get("can_downgrade"):
        title = N_("Downgrade Bazzite Portal Decky plugin")
        description = C_("Downgrade the installed plugin from {installed} to the bundled version {bundled}. Decky Loader preserves its settings.",
                         installed=status.get("installed_version", ""), bundled=status.get("bundled_version", ""))
    elif status.get("can_install"):
        title, description = N_("Install Bazzite Portal Decky plugin"), N_("Decky Loader will ask you to confirm installation of the bundled plugin.")
    elif status.get("installed_version"):
        title = N_("Bazzite Portal Decky plugin installed")
        description = status.get("error") or status.get("reason") or C_("Installed version: {version}", version=status["installed_version"])
    else:
        title = N_("Install Bazzite Portal Decky plugin")
        description = status.get("error") or status.get("reason") or N_("Decky plugin installation is unavailable.")
    owner.decky_row.set_title(format_message(title))
    owner.decky_row.set_subtitle(format_message(description))
    owner.decky_row.set_sensitive(allowed)
    owner.decky_row.set_activatable(allowed)
    if allowed:
        owner.decky_row.add_css_class("bazzite-update-cta")
    else:
        owner.decky_row.remove_css_class("bazzite-update-cta")


def _decode(value):
    result = json.loads(value.unpack()[0])
    if not isinstance(result, dict):
        raise ValueError("Invalid Decky plugin status")
    return result


def request(owner, method, parameters=None):
    if owner.closing or owner.decky_request_pending:
        return
    owner.decky_request_pending = True
    render(owner)

    def failed(error):
        if owner.closing:
            return
        owner.decky_request_pending = False
        owner.decky_status = {"error": public_error(error,
            N_("Could not manage the Decky plugin. Open Logs for details."), context="Decky plugin")}
        render(owner)

    def finished(value):
        if owner.closing:
            return
        try:
            status = _decode(value)
        except (ValueError, TypeError, AttributeError, IndexError) as error:
            failed(error)
            return
        owner.decky_request_pending = False
        owner.decky_status = status
        render(owner)

    try:
        owner.client.call(method, parameters, callback=finished, error_callback=failed)
    except Exception as error:
        failed(error)


def refresh(owner):
    request(owner, "GetDeckyStatus")


def install(owner):
    status = owner.decky_status
    if not (status.get("can_install") or status.get("can_update") or status.get("can_downgrade")):
        return
    token = status.get("bundle_token")
    if not isinstance(token, str) or not token:
        return
    # Loader owns the single confirmation and installation/reload lifecycle.
    request(owner, "InstallDecky", GLib.Variant("(s)", (token,)))
