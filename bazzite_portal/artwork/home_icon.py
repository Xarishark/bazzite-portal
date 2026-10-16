"""Preloaded SVG layers animated by GTK's frame clock, independently of SNI."""
import hashlib
import math
import os
from pathlib import Path
import tempfile
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk
from bazzite_portal.artwork.home_layers import layers
from bazzite_portal.artwork.appearance import THEMES
from bazzite_portal.artwork.frames import _render_svg

PERIOD_US = 1_536_000
_LAYER_SIZE = 64
_LAYER_BYTES = _LAYER_SIZE * _LAYER_SIZE * 4
_CACHE_VERSION = b"home-artwork-rgba-v1\0"
_CACHE_MAX_FILES = 64


def _rgba_layer(svg):
    """Rasterize one SVG layer, reusing a validated raw RGBA cache entry."""
    encoded = svg.encode("utf-8")
    cache_key = hashlib.sha256(
        _CACHE_VERSION + str(_LAYER_SIZE).encode("ascii") + b"\0" + encoded
    ).hexdigest()
    cache_dir = Path(GLib.get_user_cache_dir()) / "bazzite-portal" / "home-artwork"
    cache_path = cache_dir / (cache_key + ".rgba")

    try:
        with cache_path.open("rb") as source:
            rgba = source.read(_LAYER_BYTES + 1)
        if len(rgba) == _LAYER_BYTES:
            return _LAYER_SIZE, _LAYER_SIZE, rgba
    except OSError:
        pass

    width, height, argb = _render_svg(svg, _LAYER_SIZE)
    rgba = bytearray(len(argb))
    rgba[0::4], rgba[1::4] = argb[1::4], argb[2::4]
    rgba[2::4], rgba[3::4] = argb[3::4], argb[0::4]
    rgba = bytes(rgba)
    if width != _LAYER_SIZE or height != _LAYER_SIZE or len(rgba) != _LAYER_BYTES:
        return width, height, rgba

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            # The active icon set contains far fewer than 64 layers. Stop
            # growing the cache if stale entries from older assets accumulate.
            if sum(1 for entry in cache_dir.glob("*.rgba")) >= _CACHE_MAX_FILES:
                return width, height, rgba
        descriptor, temporary = tempfile.mkstemp(prefix=".layer-", suffix=".tmp", dir=cache_dir)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(rgba)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, cache_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError:
        # Cache failures must never prevent artwork from being prepared.
        pass
    return width, height, rgba


def _prepare():
    """Load a few fixed layers per choice in one worker, never 96 frames."""
    prepared = {}
    for theme in THEMES:
        try:
            value = layers(theme)
            for key in ("underlay", "base", "fixed_mask", "moving"):
                if key not in value:
                    continue
                value[key] = _rgba_layer(value[key])
            prepared[theme] = value
        except (ValueError, OSError, RuntimeError):
            # Missing distro assets must not prevent the application opening.
            prepared[theme] = None
    from bazzite_portal.input.prompts import preload_artwork
    return prepared, preload_artwork()


def _point(x, y):
    return Graphene.Point().init(x, y)


class LayeredIcon(GObject.Object, Gdk.Paintable):
    """Keep SVG backgrounds fixed while transforming only their moving layer."""
    def __init__(self, artwork):
        super().__init__()
        self.artwork, self.phase = artwork, 0.0

    def do_get_intrinsic_width(self):
        return 32

    def do_get_intrinsic_height(self):
        return 32

    def do_get_intrinsic_aspect_ratio(self):
        return 1.0

    def do_get_flags(self):
        return Gdk.PaintableFlags.STATIC_SIZE

    def do_snapshot(self, snapshot, width, height):
        art = self.artwork
        rect = Graphene.Rect().init(0, 0, width, height)
        def draw(key):
            if art.get(key) is not None:
                snapshot.append_texture(art[key], rect)
        def moving():
            snapshot.save()
            if art["motion"] == "rotate":
                cx, cy = art["center"]
                snapshot.translate(_point(cx * width, cy * height))
                snapshot.rotate(360 * self.phase)
                snapshot.translate(_point(-cx * width, -cy * height))
            elif art["motion"] == "bounce":
                snapshot.translate(_point(0, height * art["amplitude"] * math.sin(2 * math.pi * self.phase)))
            draw("moving")
            snapshot.restore()
        draw("underlay")
        if art.get("masked"):
            snapshot.push_mask(Gsk.MaskMode.INVERTED_ALPHA)
            draw("fixed_mask")
            moving()
            snapshot.pop()
            draw("base")
            snapshot.pop()
        else:
            draw("base")
            if art.get("moving") is not None:
                moving()


class HomeUpdateIcon:
    def __init__(self, image, executor):
        self.image = image
        self._cache = {}
        self._theme = None
        self._running = self._visible = self._closed = False
        self._timer = 0
        self._frame = 0.0
        self._started = None
        self.ready = False
        self._waiters = []
        future = executor.submit(_prepare)
        def completed(result):
            try:
                prepared = result.result()
            except Exception:
                prepared = ({}, {})
            if not self._closed:
                GLib.idle_add(self._loaded, prepared)
        future.add_done_callback(completed)

    def when_ready(self, callback):
        if self._closed:
            return
        if self.ready:
            callback()
        else:
            self._waiters.append(callback)

    def _loaded(self, prepared):
        if self._closed:
            return GLib.SOURCE_REMOVE
        prepared, prompts = prepared
        from bazzite_portal.input.prompts import install_artwork
        install_artwork(prompts)
        for theme, value in prepared.items():
            if value is None:
                continue
            for key in ("underlay", "base", "fixed_mask", "moving"):
                if key in value:
                    width, height, data = value[key]
                    value[key] = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                                      GLib.Bytes.new(data), width * 4)
            self._cache[theme] = LayeredIcon(value)
        self.ready = True
        self._apply()
        waiters, self._waiters = self._waiters, []
        for callback in waiters:
            callback()
        return GLib.SOURCE_REMOVE

    def update(self, theme, running, visible):
        if self._closed:
            return
        changed = (theme, bool(running), bool(visible)) != (self._theme, self._running, self._visible)
        self._theme, self._running, self._visible = theme, bool(running), bool(visible)
        if changed and self.ready:
            self._apply()

    def _apply(self):
        self._stop_timer()
        paintable = self._cache.get(self._theme)
        if paintable is None:
            # Permanent native fallback, selected before first presentation.
            self.image.set_from_icon_name("system-software-update-symbolic")
            return
        paintable.phase = self._frame = 0.0
        paintable.invalidate_contents()
        self.image.set_from_paintable(paintable)
        if self._running and self._visible and paintable.artwork["motion"] != "none":
            self._started = None
            self._timer = self.image.add_tick_callback(self._tick)

    def _tick(self, widget, frame_clock):
        if self._closed:
            return GLib.SOURCE_REMOVE
        now = frame_clock.get_frame_time()
        if self._started is None:
            self._started = now
        self._frame = ((now - self._started) % PERIOD_US) / PERIOD_US
        paintable = self._cache[self._theme]
        paintable.phase = self._frame
        paintable.invalidate_contents()
        return GLib.SOURCE_CONTINUE

    def _stop_timer(self):
        if self._timer:
            self.image.remove_tick_callback(self._timer)
            self._timer = 0

    def close(self):
        self._closed = True
        self._visible = False
        self._waiters.clear()
        self._stop_timer()

    running = property(lambda self: self._running)
    visible = property(lambda self: self._visible)
    timer_id = property(lambda self: self._timer)
