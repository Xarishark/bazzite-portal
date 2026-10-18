"""StatusNotifierItem and DBusMenu, using Gio only (no GTK in the agent)."""
from gi.repository import Gio, GLib
from concurrent.futures import ThreadPoolExecutor
from bazzite_portal.artwork.appearance import TrayAppearanceWatcher
from bazzite_portal import APP_ID
from bazzite_portal.core.i18n import _
from bazzite_portal.artwork.frames import (
    FRAME_COUNT,
    FRAME_INTERVAL_MS,
    TrayArtwork,
    animation_allowed,
    ANIMATED_KINDS,
    ARTWORK_KINDS,
)
from bazzite_portal.artwork.appearance import native_desktop, semantic_kind

ITEM_XML = '''<node><interface name="org.kde.StatusNotifierItem">
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="WindowId" type="u" access="read"/><property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/><property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/><property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/><property name="AttentionMovieName" type="s" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
<signal name="NewStatus"><arg type="s"/></signal>
</interface></node>'''
MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>'''


class Tray:
    def __init__(self, connection, opened, dismissed, appearance_watcher=None):
        self.connection, self.opened, self.dismissed = None, opened, dismissed
        self.kind = ""
        self.semantic = ""
        self.registered = False
        self.item_registration = None
        self.menu_registration = None
        self._appearance_generation = 0
        self._closed = False
        self._appearance_worker_active = False
        self._appearance_request = None
        self._appearance_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tray-appearance")
        self._appearance = appearance_watcher or TrayAppearanceWatcher(self._appearance_changed)
        if appearance_watcher is not None and hasattr(appearance_watcher, "changed"):
            appearance_watcher.changed = self._appearance_changed
        self._foreground = self._appearance.foreground
        try:
            self._panel_desktop = native_desktop()
        except ValueError:
            self._panel_desktop = ""
        self.artwork = TrayArtwork(foreground=self._foreground,
                                  panel_desktop=self._panel_desktop)
        self._pixmap_variants = {}
        self._tooltip_variants = {}
        self.artwork_failed = False
        self.frame = 0
        self.animation_timer = 0
        self.watch = Gio.bus_watch_name_on_connection(connection, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE, self._watcher_appeared, self._watcher_vanished)

    def close(self):
        """Stop appearance monitoring and prevent pending render results applying."""
        if self._closed:
            return
        self._closed = True
        self._appearance_generation += 1
        self._appearance.close()
        if self.watch:
            Gio.bus_unwatch_name(self.watch)
            self.watch = 0
        self._stop_animation_timer()
        self._appearance_executor.shutdown(wait=False, cancel_futures=True)

    def _appearance_changed(self, foreground):
        if self._closed or foreground == self._foreground:
            return
        self._foreground = foreground
        self._appearance_generation += 1
        generation, kind = self._appearance_generation, self.kind
        if kind not in ARTWORK_KINDS or self.artwork_failed:
            self.artwork = TrayArtwork(foreground=foreground,
                                      panel_desktop=self._panel_desktop)
            self._pixmap_variants.clear()
            self._tooltip_variants.clear()
            return
        self._request_appearance_render(generation, kind, foreground)

    def _request_appearance_render(self, generation, kind, foreground):
        if self._appearance_worker_active:
            self._appearance_request = (generation, kind, foreground)
            return
        self._appearance_worker_active = True
        old = self.artwork
        def render():
            try:
                artwork = TrayArtwork(old.svg_path, old.logo_path, old.portal_path,
                                      old.theme_paths, foreground=foreground,
                                      panel_desktop=self._panel_desktop)
                for frame in range(FRAME_COUNT if kind in ANIMATED_KINDS else 1):
                    artwork.pixmaps(kind, frame)
                result = (artwork, None)
            except Exception as error:
                result = (None, error)
            GLib.idle_add(self._appearance_rendered, generation, kind,
                          foreground, *result)
        try:
            self._appearance_executor.submit(render)
        except RuntimeError:
            self._appearance_worker_active = False

    def _appearance_rendered(self, generation, kind, foreground, artwork, error):
        self._appearance_worker_active = False
        current = (not self._closed and generation == self._appearance_generation
                   and kind == self.kind and foreground == self._foreground)
        if current and error is None and artwork is not None:
            self.artwork = artwork
            self._pixmap_variants.clear()
            self._tooltip_variants.clear()
            self._sync_animation_timer()
            if self.connection is not None and not self.connection.is_closed():
                for signal in ("NewIcon", "NewToolTip"):
                    self.connection.emit_signal(None, "/StatusNotifierItem",
                                                "org.kde.StatusNotifierItem", signal, None)
        request, self._appearance_request = self._appearance_request, None
        if request is not None and not self._closed:
            newest = (self._appearance_generation, self.kind, self._foreground)
            if newest[1] in ARTWORK_KINDS and not self.artwork_failed:
                self._request_appearance_render(*newest)
        return GLib.SOURCE_REMOVE

    def _watcher_appeared(self, *_):
        self.registered = False
        if self.kind:
            self._register()
        self._sync_animation_timer()

    def _watcher_vanished(self, *_):
        self.registered = False
        self._stop_animation_timer()

    def _register(self):
        if self.connection is None:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        if self.item_registration is None:
            self.item_registration = self.connection.register_object("/StatusNotifierItem", Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0], self._item_method, self._item_property, None)
            self.menu_registration = self.connection.register_object("/Menu", Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0], self._menu_method, self._menu_property, None)
        self.connection.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem", GLib.Variant("(s)", ("/StatusNotifierItem",)), None, Gio.DBusCallFlags.NONE, 3000, None, self._registered)

    def _registered(self, connection, result):
        try:
            connection.call_finish(result)
            self.registered = connection is self.connection and bool(self.kind)
        except GLib.Error:
            self.registered = False
        self._sync_animation_timer()

    def _stop_animation_timer(self):
        if self.animation_timer:
            GLib.source_remove(self.animation_timer)
            self.animation_timer = 0

    def _sync_animation_timer(self):
        connected = self.connection is not None and not self.connection.is_closed()
        if animation_allowed(self.kind, self.registered, connected,
                             not self.artwork_failed):
            if not self.animation_timer:
                self.animation_timer = GLib.timeout_add(FRAME_INTERVAL_MS, self._advance_frame)
        else:
            self._stop_animation_timer()

    def _advance_frame(self):
        connected = self.connection is not None and not self.connection.is_closed()
        if not animation_allowed(self.kind, self.registered, connected,
                                 not self.artwork_failed):
            self.animation_timer = 0
            return GLib.SOURCE_REMOVE
        self.frame = (self.frame + 1) % FRAME_COUNT
        try:
            self.connection.emit_signal(None, "/StatusNotifierItem",
                                        "org.kde.StatusNotifierItem", "NewIcon", None)
        except GLib.Error:
            self.animation_timer = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _pixmaps(self, kind):
        if self.artwork_failed:
            return []
        try:
            return self.artwork.pixmaps(kind, self.frame)
        except Exception:
            # Preserve a usable theme icon if the optional SVG renderer cannot
            # load the artwork on this image.
            self.artwork_failed = True
            self._stop_animation_timer()
            return []

    def _pixmap_variant(self, kind, frame=None, allow_failed=False):
        frame = self.frame if frame is None else frame
        if self.artwork_failed and not allow_failed:
            kind = ""
        artwork = self.artwork
        key = (kind, frame % FRAME_COUNT if kind in ANIMATED_KINDS else 0,
               artwork.foreground)
        if key not in self._pixmap_variants:
            pixels = artwork.pixmaps(kind, key[1]) if kind in ARTWORK_KINDS and (allow_failed or not self.artwork_failed) else []
            # Byte arrays already have the exact GVariant `ay` wire layout.
            # Preserve them as bytes instead of converting each pixel in Python.
            rows = [GLib.Variant.new_tuple(GLib.Variant("i", width), GLib.Variant("i", height),
                GLib.Variant.new_from_bytes(GLib.VariantType.new("ay"), GLib.Bytes.new(data), True))
                for width, height, data in pixels]
            self._pixmap_variants[key] = GLib.Variant.new_array(GLib.VariantType.new("(iiay)"), rows)
        return self._pixmap_variants[key]

    def prepare_artwork(self, kind):
        """Precompute raster and D-Bus values in the preview worker."""
        # Keep failures local to the worker result. A stale preparation must
        # not mark whichever theme is currently displayed as failed.
        self.artwork.pixmaps(kind)
        for frame in range(FRAME_COUNT if kind in ANIMATED_KINDS else 1):
            self._pixmap_variant(kind, frame, allow_failed=True)

    def _tooltip_variant(self, title, description):
        key = (self.kind, self.semantic, self.artwork_failed, self.artwork.foreground,
               title, description)
        if key not in self._tooltip_variants:
            pixmaps = self._pixmap_variant(self.kind, 0)
            icon = "" if pixmaps.n_children() else {
                "updating": "system-software-update-symbolic", "restart": "system-reboot-symbolic",
                "failed": "dialog-error-symbolic"}.get(self.semantic, "")
            self._tooltip_variants[key] = GLib.Variant.new_tuple(
                GLib.Variant("s", icon), pixmaps, GLib.Variant("s", title), GLib.Variant("s", description))
        return self._tooltip_variants[key]

    def update(self, kind, semantic=None, fallback_only=False):
        self._appearance_generation += 1
        previous = self.kind
        previous_semantic = self.semantic
        previous_failed = self.artwork_failed
        self.artwork_failed = bool(fallback_only)
        self.kind = kind
        self.semantic = semantic or semantic_kind(kind)
        if fallback_only:
            self.artwork_failed = True
        if kind in ARTWORK_KINDS and not self.artwork_failed and self.artwork.foreground != self._foreground:
            self._request_appearance_render(self._appearance_generation, kind, self._foreground)
        if not kind:
            self._stop_animation_timer()
            # Unregistering the item removes it from both Plasma and GNOME hosts.
            if self.item_registration is not None:
                self.connection.unregister_object(self.item_registration)
                self.connection.unregister_object(self.menu_registration)
                # Hosts track the item's unique bus owner. Closing this dedicated
                # connection reliably removes the indicator, without stopping the agent.
                self.connection.close_sync(None)
                self.connection = None
                self.item_registration = self.menu_registration = None
            self.registered = False
        elif not self.registered:
            if kind in ANIMATED_KINDS and previous != kind:
                self.frame = 0
            self._register()
        elif kind in ANIMATED_KINDS and previous != kind:
            self.frame = 0
        self._sync_animation_timer()
        if kind and (previous != kind or previous_semantic != self.semantic or previous_failed != self.artwork_failed):
            for signal in ("NewIcon", "NewTitle", "NewToolTip"):
                self.connection.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", signal, None)
            self.connection.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewStatus", GLib.Variant("(s)", ("Active",)))

    def _item_method(self, connection, sender, path, interface, method, parameters, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.opened()
        invocation.return_value(None)

    def _item_property(self, connection, sender, path, interface, name):
        title, description, fallback_icon = {
            "updating": (_("Updating"), _('Updates are running. Open {app_name} for progress.').format(app_name='Bazzite Portal'), "system-software-update-symbolic"),
            "restart": (_("Restart pending"), _('{bazzite} will boot into the new version on your next restart.').format(bazzite='Bazzite'), "system-reboot-symbolic"),
            "failed": (_("Update failed"), _('An update failed. Open {app_name} for details.').format(app_name='Bazzite Portal'), "dialog-error-symbolic"),
        }.get(self.semantic, ("Bazzite", "", "system-software-update-symbolic"))
        pixmaps = self._pixmaps(self.kind) if self.kind in ARTWORK_KINDS else []
        if name in ("IconPixmap", "AttentionIconPixmap"):
            return self._pixmap_variant(self.kind)
        if name == "ToolTip":
            return self._tooltip_variant(title, description)
        icon = "" if pixmaps else fallback_icon
        values = {"Category": ("s", "SystemServices"), "Id": ("s", APP_ID), "Title": ("s", title), "Status": ("s", "Active"), "WindowId": ("u", 0), "IconName": ("s", icon), "IconPixmap": ("a(iiay)", pixmaps), "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []), "AttentionIconName": ("s", icon), "AttentionIconPixmap": ("a(iiay)", pixmaps), "AttentionMovieName": ("s", ""), "ToolTip": ("(sa(iiay)ss)", (icon, pixmaps, title, description)), "ItemIsMenu": ("b", False), "Menu": ("o", "/Menu")}
        signature, value = values[name]
        return GLib.Variant(signature, value)

    @staticmethod
    def _properties(index):
        props = {"enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}
        if index == 0:
            props["children-display"] = GLib.Variant("s", "submenu")
        else:
            props["label"] = GLib.Variant("s", _("Open") if index == 1 else _("Dismiss"))
        return props

    def _menu_property(self, connection, sender, path, interface, name):
        signature, value = {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"), "IconThemePath": ("as", [])}[name]
        return GLib.Variant(signature, value)

    def _event(self, index, event):
        if event == "clicked":
            if index == 1:
                self.opened()
            elif index == 2:
                self.dismissed()

    def _menu_method(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        if method == "GetLayout":
            parent, depth, names = args
            children = [GLib.Variant("(ia{sv}av)", (index, self._properties(index), [])) for index in (1, 2)] if parent == 0 and depth != 0 else []
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, (parent, self._properties(parent), children))))
        elif method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(index, self._properties(index)) for index in args[0]],)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (self._properties(args[0]).get(args[1], GLib.Variant("s", "")),)))
        elif method == "Event":
            self._event(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for event in args[0]:
                self._event(event[0], event[1])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
