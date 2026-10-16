"""Pure notification-area theme selection and semantic state mapping."""

from bazzite_portal.core.i18n import N_

import os
import re
import configparser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


THEMES = ("native", "spinning", "bouncing", "spinning-mono",
          "bouncing-mono")
THEME_NAMES = (
    ("native", N_("Native")),
    ("spinning", N_("Spinning")),
    ("bouncing", N_("Bouncing")),
    ("spinning-mono", N_("Spinning Mono")),
    ("bouncing-mono", N_("Bouncing Mono")),
)


def native_desktop(environ=None):
    """Resolve KDE Plasma or GNOME from desktop session tokens."""
    environ = os.environ if environ is None else environ
    value = (environ.get("XDG_CURRENT_DESKTOP", "") + ":" +
             environ.get("DESKTOP_SESSION", "")).lower()
    tokens = set(re.findall(r"[a-z0-9]+", value))
    kde = bool(tokens & {"kde", "plasma", "plasmawayland", "plasmax11"})
    gnome = "gnome" in tokens
    if kde != gnome:
        return "kde" if kde else "gnome"
    raise ValueError("Native icons require a recognized KDE Plasma or GNOME session.")


def artwork_kind(theme, statuskind, environ=None):
    """Map a saved theme and semantic status to the renderer's artwork kind."""
    if theme not in THEMES:
        theme = "spinning"
    if statuskind not in ("updating", "restart", "failed"):
        raise ValueError("unknown notification status: " + str(statuskind))
    if theme == "native":
        desktop = native_desktop(environ)
        state = "update" if statuskind == "updating" else statuskind
        return "native-" + desktop + "-" + state
    mono = theme.endswith("-mono")
    if statuskind == "updating":
        result = "bouncing" if theme.startswith("bouncing") else "updating"
    else:
        result = statuskind
    return result + ("-mono" if mono else "")


def semantic_kind(newartkind):
    """Return updating/restart/failed semantics for a renderer artwork kind."""
    if newartkind.startswith("native-"):
        suffix = newartkind.rsplit("-", 1)[-1]
        return "updating" if suffix == "update" else suffix
    base = newartkind.removesuffix("-mono")
    if base in ("updating", "bouncing"):
        return "updating"
    return base if base in ("restart", "failed") else ""



LIGHT_FOREGROUND = "#000000"
DARK_FOREGROUND = "#fcfcfc"
MAX_CONFIG_BYTES = 64 * 1024


def select_foreground(color_scheme=None, background=None, gtk_theme=None):
    """Choose black for light appearance and white for dark appearance."""
    scheme = str(color_scheme or "").strip().casefold()
    if scheme in {"prefer-dark", "dark", "1", "true"}:
        return DARK_FOREGROUND
    if scheme in {"prefer-light", "light", "2", "false"}:
        return LIGHT_FOREGROUND
    if background:
        numbers = re.findall(r"\d+(?:\.\d+)?", str(background))
        if len(numbers) >= 3:
            red, green, blue = (float(value) for value in numbers[:3])
            # KDE colors are 16-bit; accept both 8-bit and normalized values.
            if max(red, green, blue) <= 1:
                red, green, blue = red * 255, green * 255, blue * 255
            elif max(red, green, blue) > 255:
                red, green, blue = red / 257, green / 257, blue / 257
            luminance = .2126 * red + .7152 * green + .0722 * blue
            return LIGHT_FOREGROUND if luminance >= 128 else DARK_FOREGROUND
    theme = str(gtk_theme or "").casefold()
    if "dark" in theme or "twilight" in theme:
        return DARK_FOREGROUND
    return LIGHT_FOREGROUND


def read_kde_background(path=None):
    """Read KDE's window background from a bounded kdeglobals file."""
    path = Path(path or Path.home() / ".config" / "kdeglobals")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        config = configparser.ConfigParser()
        config.read_string(raw.decode("utf-8", errors="replace"))
        return config.get("Colors:Window", "BackgroundNormal", fallback=None)
    except (OSError, configparser.Error):
        return None


class TrayAppearanceWatcher:
    """Observe KDE config replacement and optional GNOME GSettings changes."""
    def __init__(self, changed, kde_path=None):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.changed = changed
        self.closed = False
        self.kde_path = Path(kde_path or Path.home() / ".config" / "kdeglobals")
        try:
            self._use_kde = kde_path is not None or native_desktop() == "kde"
        except ValueError:
            self._use_kde = kde_path is not None
        self.kde_background = None
        self.color_scheme = None
        self.gtk_theme = None
        self.foreground = LIGHT_FOREGROUND
        self._generation = 0
        self._debounce = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tray-settings")
        self._monitor = None
        self._settings = []
        if self._use_kde:
            try:
                directory = Gio.File.new_for_path(str(self.kde_path.parent))
                self._monitor = directory.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                self._monitor.connect("changed", self._file_changed)
            except GLib.Error:
                pass
            self._queue_kde_read()
        else:
            self._load_gnome_settings()
            self.GLib.idle_add(self._kde_read_done, self._generation, None)

    def _load_gnome_settings(self):
        source = self.Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.interface", True) if source else None
        if schema is None:
            return
        try:
            settings = self.Gio.Settings.new_full(schema, None, None)
            for key, attribute in (("color-scheme", "color_scheme"),
                                   ("gtk-theme", "gtk_theme")):
                if not schema.has_key(key):
                    continue
                setattr(self, attribute, settings.get_string(key))
                settings.connect("changed::" + key, self._gnome_changed, attribute)
            self._settings.append(settings)
        except self.GLib.Error:
            pass

    def _gnome_changed(self, settings, key, attribute):
        if self.closed:
            return
        setattr(self, attribute, settings.get_string(key))
        self._apply()

    def _file_changed(self, _monitor, file, other_file, _event):
        if self.closed:
            return
        paths = {file.get_path() if file else None,
                 other_file.get_path() if other_file else None}
        if str(self.kde_path) not in paths:
            return
        self._generation += 1
        if self._debounce:
            self.GLib.source_remove(self._debounce)
        self._debounce = self.GLib.timeout_add(120, self._queue_kde_read)

    def _queue_kde_read(self):
        self._debounce = 0
        if self.closed:
            return self.GLib.SOURCE_REMOVE
        generation = self._generation
        future = self._executor.submit(read_kde_background, self.kde_path)
        def completed(result):
            if not result.cancelled():
                self.GLib.idle_add(self._kde_read_done, generation, result.result())
        future.add_done_callback(completed)
        return self.GLib.SOURCE_REMOVE

    def _kde_read_done(self, generation, background):
        if self.closed or generation != self._generation:
            return self.GLib.SOURCE_REMOVE
        self.kde_background = background
        self._apply()
        return self.GLib.SOURCE_REMOVE

    def _apply(self):
        if self.kde_background:
            foreground = select_foreground(background=self.kde_background)
        else:
            foreground = select_foreground(self.color_scheme, gtk_theme=self.gtk_theme)
        if foreground != self.foreground:
            self.foreground = foreground
            self.changed(foreground)

    def close(self):
        self.closed = True
        self.changed = lambda _foreground: None
        if self._debounce:
            self.GLib.source_remove(self._debounce)
            self._debounce = 0
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        for settings in self._settings:
            settings.disconnect_by_func(self._gnome_changed)
        self._settings.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)




def gaming_mode(environ=None):
    environ = os.environ if environ is None else environ
    desktops = environ.get("XDG_CURRENT_DESKTOP", "").casefold().split(":")
    return ("gamescope" in desktops
            or environ.get("XDG_SESSION_DESKTOP", "").casefold() == "gamescope"
            or environ.get("SteamGamepadUI") == "1")
