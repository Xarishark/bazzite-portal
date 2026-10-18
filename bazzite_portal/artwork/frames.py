"""Bounded SVG artwork rendering for the StatusNotifierItem tray icon.

GdkPixbuf is imported only when an icon is first requested. This module does
not import GTK and uses the PyGObject runtime already required by the app.
"""
from pathlib import Path
import math
import xml.etree.ElementTree as ET


FRAME_COUNT = 96
FRAME_INTERVAL_MS = 16
ICON_SIZES = (32, 64)
ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "indicator"
DEFAULT_SVG = ASSET_DIR / "bazzite-update.svg"
DEFAULT_LOGO = ASSET_DIR / "bazzite-logo.svg"
DEFAULT_PORTAL = ASSET_DIR / "bazzite-portal.svg"
KDE_UPDATE = ASSET_DIR / "kde-update.svg"
GNOME_UPDATE = ASSET_DIR / "gnome-update.svg"
KDE_RESTART = ASSET_DIR / "kde-restart.svg"
KDE_FAILED = ASSET_DIR / "kde-failed.svg"
GNOME_RESTART = ASSET_DIR / "gnome-restart.svg"
GNOME_FAILED = ASSET_DIR / "gnome-failed.svg"
MONO = "#fcfcfc"
ARTWORK_KINDS = (
    "updating", "bouncing", "kde", "gnome", "restart", "failed",
    "updating-mono", "bouncing-mono", "restart-mono", "failed-mono",
    "native-kde-update", "native-gnome-update",
    "native-kde-restart", "native-kde-failed",
    "native-gnome-restart", "native-gnome-failed",
)
ANIMATED_KINDS = ("updating", "bouncing", "kde", "updating-mono",
                  "bouncing-mono", "native-kde-update")
NATIVE_PATHS = {
    "native-kde-restart": KDE_RESTART, "native-kde-failed": KDE_FAILED,
    "native-gnome-restart": GNOME_RESTART, "native-gnome-failed": GNOME_FAILED,
}
MAX_SVG_BYTES = 256 * 1024


def animation_allowed(kind, registered, connected, artwork_ready=True):
    """Return whether a timer may run for the current visible tray state."""
    return (kind in ANIMATED_KINDS and bool(registered) and bool(connected)
            and bool(artwork_ready))


def kde_svg_frames(source, frame_count=FRAME_COUNT):
    """Spin only Breeze's white busy ring, leaving the blue update badge fixed."""
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise ValueError("invalid KDE update SVG") from error
    if root.tag.split("}")[-1] != "svg" or not root.get("viewBox"):
        raise ValueError("KDE update SVG must have an svg root and viewBox")
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    group = next((item for item in root.iter()
                  if item.tag.split("}")[-1] == "g"
                  and item.get("transform") == "translate(-10.843816,-3.717558)"), None)
    paths = ([item for item in group.iter() if item.tag.split("}")[-1] == "path"]
             if group is not None else [])
    if len(paths) != 2:
        raise ValueError("unexpected KDE update SVG structure")
    center_x, center_y = "26.843816", "19.717558"
    frames = []
    for index in range(frame_count):
        degrees = 360 * index / frame_count
        angle = str(int(degrees)) if degrees.is_integer() else ("%.3f" % degrees).rstrip("0").rstrip(".")
        frame = ET.fromstring(ET.tostring(root, encoding="unicode"))
        frame_group = next(item for item in frame.iter()
                           if item.tag.split("}")[-1] == "g"
                           and item.get("transform") == "translate(-10.843816,-3.717558)")
        frame_paths = [item for item in frame_group.iter()
                       if item.tag.split("}")[-1] == "path"]
        frame_paths[1].set("transform", "rotate(%s %s %s)" % (angle, center_x, center_y))
        frames.append(ET.tostring(frame, encoding="unicode"))
    return tuple(frames)


def gnome_spinning_svg(source):
    """Reduce classic spinner padding inside GNOME's small panel icon slot."""
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    try:
        root = ET.fromstring(source)
        x, y, width, height = map(float, root.get("viewBox", "").split())
    except (ET.ParseError, ValueError) as error:
        raise ValueError("invalid spinning tray SVG viewBox") from error
    if (root.tag.split("}")[-1] != "svg"
            or not all(math.isfinite(value) for value in (x, y, width, height))
            or width <= 0 or height <= 0):
        raise ValueError("invalid spinning tray SVG viewBox")
    inset = 1 / 16
    root.set("viewBox", " ".join(format(value, ".12g") for value in (
        x + width * inset, y + height * inset,
        width * (1 - 2 * inset), height * (1 - 2 * inset))))
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="unicode")


def rotated_svg_frames(source, frame_count=FRAME_COUNT, monochrome=False,
                       foreground=MONO):
    """Rotate updater cutouts in a mask while the gradient circle stays fixed."""
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise ValueError("invalid Bazzite updater SVG") from error
    path = next((item for item in root.iter() if item.tag.split("}")[-1] == "path"
                 and item.get("id") == "path3"), None)
    if path is None:
        raise ValueError("unexpected Bazzite updater SVG structure")
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    parts = _path_subpaths(path.get("d", ""))
    if len(parts) < 2:
        raise ValueError("unexpected Bazzite updater compound path")
    frames = []
    for index in range(frame_count):
        degrees = 360 * index / frame_count
        angle = str(int(degrees)) if degrees.is_integer() else ("%.3f" % degrees).rstrip("0").rstrip(".")
        frame = ET.fromstring(ET.tostring(root, encoding="unicode"))
        moving = next(item for item in frame.iter() if item.tag.split("}")[-1] == "path"
                      and item.get("id") == "path3")
        if monochrome:
            _monochrome(frame, foreground=foreground)
            ellipse = next((item for item in frame.iter()
                            if item.tag.split("}")[-1] == "ellipse"
                            and item.get("id") == "path2"), None)
            if ellipse is not None:
                ellipse.set("display", "none")
                ellipse.set("style", "display:none")
            moving.set("style", "display:inline;fill:%s" % foreground)
            moving.attrib.pop("filter", None)
        moving.set("d", parts[0])
        moving.set("mask", "url(#tray-motion-mask)")
        _add_motion_mask(frame, parts[0], "", " ".join(parts[1:]),
                         "rotate(%s 8.5 10.283)" % angle)
        frames.append(ET.tostring(frame, encoding="unicode"))
    return tuple(frames)


def _path_subpaths(data):
    """Split absolute-M SVG compound paths at subpath boundaries."""
    import re
    matches = list(re.finditer(r"(?<![A-Za-z])M\s", data))
    if not matches or matches[0].start() != len(data) - len(data.lstrip()):
        raise ValueError("unexpected compound SVG path")
    return [data[match.start():matches[index + 1].start()].strip()
            if index + 1 < len(matches) else data[match.start():].strip()
            for index, match in enumerate(matches)]


def _add_motion_mask(root, outer, fixed_holes, moving_hole, moving_transform=None):
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    tag = "{%s}" % namespace if namespace else ""
    defs = next((element for element in root if element.tag.split("}")[-1] == "defs"), None)
    if defs is None:
        defs = ET.Element(tag + "defs")
        root.insert(0, defs)
    mask = ET.SubElement(defs, tag + "mask", {
        "id": "tray-motion-mask", "maskUnits": "userSpaceOnUse",
        "x": "-2", "y": "-2", "width": "24", "height": "24",
    })
    ET.SubElement(mask, tag + "path", {"d": outer, "fill": "white"})
    if fixed_holes:
        ET.SubElement(mask, tag + "path", {"d": fixed_holes, "fill": "black"})
    if moving_hole:
        group = ET.SubElement(mask, tag + "g", {"transform": moving_transform or ""})
        ET.SubElement(group, tag + "path", {"d": moving_hole, "fill": "black"})


def bouncing_svg_frames(source, frame_count=FRAME_COUNT, monochrome=False,
                        foreground=MONO):
    """Animate only the arrow cutout in the installed Portal icon."""
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if isinstance(source, bytes):
        if len(source) > MAX_SVG_BYTES:
            raise ValueError("Bazzite logo SVG exceeds the size limit")
        source = source.decode("utf-8")
    if len(source.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError("Bazzite logo SVG exceeds the size limit")
    try:
        logo = ET.fromstring(source)
    except ET.ParseError as error:
        raise ValueError("invalid Bazzite logo SVG") from error
    if logo.tag.split("}")[-1] != "svg" or not logo.get("viewBox"):
        raise ValueError("Bazzite logo SVG must have an svg root and viewBox")
    namespace = logo.tag[1:].split("}", 1)[0] if logo.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    tag = ("{%s}" % namespace) if namespace else ""
    path = next((item for item in logo.iter() if item.tag.split("}")[-1] == "path"
                 and item.get("id") == "path4"), None)
    if path is None:
        raise ValueError("Portal SVG is missing path4")
    parts = _path_subpaths(path.get("d", ""))
    if len(parts) < 3:
        raise ValueError("unexpected Portal compound path")
    outer, arrow, tray = parts[0], parts[1], " ".join(parts[2:])
    logo.set("x", "0")
    logo.set("y", "0")
    logo.set("width", "32")
    logo.set("height", "32")
    frames = []
    for index in range(frame_count):
        phase = 2 * math.pi * index / frame_count
        offset = 0.5 * math.sin(phase)
        canvas = ET.fromstring(ET.tostring(logo, encoding="unicode"))
        if monochrome:
            _monochrome(canvas, foreground=foreground)
            for background in canvas:
                if background.tag.split("}")[-1] == "ellipse":
                    background.set("style", "display:none")
                    background.set("display", "none")
        moving = next(item for item in canvas.iter() if item.tag.split("}")[-1] == "path"
                      and item.get("id") == "path4")
        moving.set("d", outer)
        moving.set("mask", "url(#tray-motion-mask)")
        _add_motion_mask(canvas, outer, tray, arrow, "translate(0 %.3f)" % offset)
        frames.append(ET.tostring(canvas, encoding="unicode"))
    return tuple(frames)


def badged_svg(source, kind, monochrome=False, foreground=MONO):
    """Return classic Bazzite SVG art with a restart or failure status badge."""
    colors = {"restart": "#2ec27e", "failed": "#e01b24"}
    if kind not in colors:
        raise ValueError("unknown badge state: " + str(kind))
    if isinstance(source, bytes):
        if len(source) > MAX_SVG_BYTES:
            raise ValueError("Bazzite logo SVG exceeds the size limit")
        source = source.decode("utf-8")
    if len(source.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError("Bazzite logo SVG exceeds the size limit")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise ValueError("invalid Bazzite logo SVG") from error
    if root.tag.split("}")[-1] != "svg" or not root.get("viewBox"):
        raise ValueError("Bazzite logo SVG must have an svg root and viewBox")
    if monochrome:
        # The colored logo contains several decorative paths. Its second path
        # is the actual even-odd B mark; render only that path in mono mode.
        paths = [item for item in root.iter() if item.tag.split("}")[-1] == "path"]
        mark = next((item for item in paths if "evenodd" in (
            item.get("fill-rule", "") + " " + item.get("clip-rule", "") + " " +
            item.get("style", "")).lower()), None)
        if mark is None and len(paths) >= 2:
            mark = paths[1]
        if mark is not None:
            # Reparent the selected path so defs and decorative siblings never
            # paint in the monochrome icon.
            viewbox = root.get("viewBox")
            svg_tag = root.tag
            root.clear()
            root.tag = svg_tag
            root.set("viewBox", viewbox)
            root.append(mark)
        _monochrome(root, foreground=foreground)
        if mark is not None:
            mark.set("fill", foreground)
            mark.set("fill-opacity", "1")
        # Ignore background ellipses in alternate source artwork.
        for element in root.iter():
            if element.tag.split("}")[-1] == "ellipse":
                element.set("display", "none")
                style = element.get("style", "")
                declarations = [item for item in style.split(";")
                                if item.strip() and not item.strip().startswith("display:")]
                declarations.append("display:none")
                element.set("style", ";".join(declarations))
    root.set("width", "32")
    root.set("height", "32")
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    svg_tag = "{%s}svg" % namespace if namespace else "svg"
    canvas = ET.Element(svg_tag, {"width": "32", "height": "32", "viewBox": "0 0 32 32"})
    canvas.append(root)
    circle_tag = "{%s}circle" % namespace if namespace else "circle"
    ET.SubElement(canvas, circle_tag, {
        "cx": "25", "cy": "7", "r": "5", "fill": colors[kind],
        "stroke": "white", "stroke-width": "1.5",
    })
    return ET.tostring(canvas, encoding="unicode")


def _monochrome(root, foreground=MONO):
    """Recolor visible SVG paint while retaining geometry, opacity and holes."""
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    masked = set()
    for mask in (item for item in root.iter() if item.tag.split("}")[-1] == "mask"):
        masked.update(mask.iter())
    for element in root.iter():
        if element in masked:
            continue
        name = element.tag.split("}")[-1]
        if name == "filter":
            parent = next((p for p in root.iter() if element in list(p)), None)
            if parent is not None:
                parent.remove(element)
            continue
        if name == "stop":
            element.set("stop-color", foreground)
        for key in ("fill", "stroke"):
            value = element.get(key)
            if value and value.lower() not in ("none", "transparent") and not value.startswith("url("):
                element.set(key, foreground)
        style = element.get("style")
        if style:
            declarations = []
            for item in style.split(";"):
                if ":" not in item:
                    continue
                key, value = item.split(":", 1)
                key, value = key.strip(), value.strip()
                if key in ("fill", "stroke", "stop-color") and value not in ("none", "transparent"):
                    value = foreground
                elif key == "filter":
                    continue
                declarations.append(key + ":" + value)
            element.set("style", ";".join(declarations))
    return root


def _native_kde_frames(source, frame_count=FRAME_COUNT, foreground=MONO):
    """Recolor the installed KDE busy motif for a monochrome variant."""
    frames = kde_svg_frames(source, frame_count)
    output = []
    for svg in frames:
        root = ET.fromstring(svg)
        group = next((item for item in root.iter()
                      if item.tag.split("}")[-1] == "g"
                      and item.get("transform") == "translate(-10.843816,-3.717558)"), None)
        paths = ([item for item in group if item.tag.split("}")[-1] == "path"]
                 if group is not None else [])
        # Keep the source's main arrow and rotating ring. Remove only the blue
        # badge path inside Breeze's translated group.
        if len(paths) != 2:
            raise ValueError("unexpected KDE update SVG structure")
        group.remove(paths[0])
        _monochrome(root, foreground=foreground)
        output.append(ET.tostring(root, encoding="unicode"))
    return tuple(output)


def _native_failure_svg(update_source, failure_source, kde=False, foreground=MONO):
    """Keep the native update symbol and place its native error mark in the badge."""
    try:
        root = ET.fromstring(update_source)
        failure = ET.fromstring(failure_source)
    except ET.ParseError as error:
        raise ValueError("invalid native update or failure SVG") from error
    if root.tag.split("}")[-1] != "svg" or failure.tag.split("}")[-1] != "svg":
        raise ValueError("native update and failure artwork must be SVGs")
    if not root.get("viewBox") or not failure.get("viewBox"):
        raise ValueError("native update and failure SVGs require viewBoxes")
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    if kde:
        group = next((item for item in root.iter()
                      if item.tag.split("}")[-1] == "g"
                      and item.get("transform") == "translate(-10.843816,-3.717558)"), None)
        paths = ([item for item in group if item.tag.split("}")[-1] == "path"]
                 if group is not None else [])
        if len(paths) != 2:
            raise ValueError("unexpected KDE update SVG structure")
        # Keep the busy symbol's arrow, remove its disk and spinner, and reuse
        # that exact badge position for the installed native failure symbol.
        group.remove(paths[0])
        group.remove(paths[1])
        x, y, size = "11", "11", "10"
    else:
        viewbox = [float(value) for value in root.get("viewBox").split()]
        if len(viewbox) != 4:
            raise ValueError("invalid native update viewBox")
        x, y, size = str(viewbox[0] + viewbox[2] * .58), str(viewbox[1] + viewbox[3] * .58), str(viewbox[2] * .38)
    nested_tag = "{%s}svg" % namespace if namespace else "svg"
    nested = ET.SubElement(root, nested_tag, {
        "x": x, "y": y, "width": size, "height": size,
        "viewBox": failure.get("viewBox"), "preserveAspectRatio": "xMidYMid meet",
    })
    for child in list(failure):
        nested.append(child)
    _monochrome(root, foreground=foreground)
    return ET.tostring(root, encoding="unicode")


def rgba_to_argb(pixels, width, height, rowstride, channels, has_alpha):
    """Pack GdkPixbuf RGB(A) rows as SNI network-order ARGB bytes."""
    if width < 1 or height < 1 or channels not in (3, 4):
        raise ValueError("invalid pixbuf dimensions or channel count")
    minimum_length = rowstride * (height - 1) + width * channels
    if rowstride < width * channels or len(pixels) < minimum_length:
        raise ValueError("pixbuf pixel buffer is shorter than its dimensions")
    output = bytearray(width * height * 4)
    target = 0
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            source = row + x * channels
            red, green, blue = pixels[source:source + 3]
            alpha = pixels[source + 3] if has_alpha and channels == 4 else 255
            output[target:target + 4] = bytes((alpha, red, green, blue))
            target += 4
    return bytes(output)


def _render_svg(svg, size):
    """Rasterize one SVG using the installed GdkPixbuf SVG loader."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.set_size(size, size)
    loader.write(svg.encode("utf-8"))
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise RuntimeError("GdkPixbuf did not render the tray SVG")
    data = rgba_to_argb(pixbuf.get_pixels(), pixbuf.get_width(), pixbuf.get_height(),
                        pixbuf.get_rowstride(), pixbuf.get_n_channels(),
                        pixbuf.get_has_alpha())
    return (pixbuf.get_width(), pixbuf.get_height(), data)


class TrayArtwork:
    """One tray instance's bounded cache of running and pending artwork."""

    def __init__(self, svg_path=DEFAULT_SVG, logo_path=DEFAULT_LOGO,
                 portal_path=DEFAULT_PORTAL, theme_paths=None, foreground=MONO,
                 panel_desktop=""):
        self.svg_path = Path(svg_path)
        self.logo_path = Path(logo_path)
        self.portal_path = Path(portal_path)
        self.theme_paths = theme_paths or {"kde": KDE_UPDATE, "gnome": GNOME_UPDATE}
        self.foreground = foreground
        self.panel_desktop = panel_desktop
        self._svg = None
        self._running = None
        self._pending = None
        self._failed = None
        self._bouncing = None
        self._pending_svg = None
        self._failed_svg = None
        self._bouncing_svg = None
        self._themes = {}
        self._native = {}
        self._mono_running = None
        self._mono_bouncing = None
        self._mono_static = {}

    def _source(self):
        if self._svg is None:
            self._svg = self.svg_path.read_text(encoding="utf-8")
        return self._svg

    def _logo_source(self, kind):
        cache_name = {"restart": "_pending_svg", "failed": "_failed_svg",
                      "bouncing": "_bouncing_svg", "bouncing-mono": "_bouncing_svg"}.get(kind)
        if cache_name is None:
            raise ValueError("unknown logo artwork state: " + str(kind))
        cached = getattr(self, cache_name)
        if cached is None:
            source_path = self.portal_path if kind in ("bouncing", "bouncing-mono") else self.logo_path
            with source_path.open("rb") as logo_file:
                source = logo_file.read(MAX_SVG_BYTES + 1)
            if len(source) > MAX_SVG_BYTES:
                raise ValueError("Bazzite logo SVG exceeds the size limit")
            if kind in ("bouncing", "bouncing-mono"):
                value = source.decode("utf-8") if isinstance(source, bytes) else source
            else:
                value = badged_svg(source, kind)
            setattr(self, cache_name, value)
        return getattr(self, cache_name)

    def _theme_source(self, kind):
        if kind not in self.theme_paths:
            raise ValueError("unknown tray artwork state: " + str(kind))
        with self.theme_paths[kind].open("rb") as theme_file:
            source = theme_file.read(MAX_SVG_BYTES + 1)
        if len(source) > MAX_SVG_BYTES:
            raise ValueError("theme update SVG exceeds the size limit")
        return source.decode("utf-8")

    def _native_source(self, kind):
        path = NATIVE_PATHS.get(kind, self.theme_paths.get(kind))
        if path is None:
            raise ValueError("unknown tray artwork state: " + str(kind))
        with Path(path).open("rb") as artwork_file:
            source = artwork_file.read(MAX_SVG_BYTES + 1)
        if len(source) > MAX_SVG_BYTES:
            raise ValueError("native theme SVG exceeds the size limit")
        return source.decode("utf-8")

    @staticmethod
    def _render_set(svg):
        return tuple((size, size, _render_svg(svg, size)[2]) for size in ICON_SIZES)

    def pixmaps(self, kind, frame=0):
        """Return SNI ``a(iiay)`` rows, caching all running frames once."""
        if kind == "updating-mono":
            if self._mono_running is None:
                frames = rotated_svg_frames(self._source(), monochrome=True,
                                            foreground=self.foreground)
                self._mono_running = tuple(self._render_set(svg) for svg in frames)
            return self._mono_running[frame % FRAME_COUNT]
        if kind == "native-kde-update":
            if kind not in self._native:
                frames = _native_kde_frames(self._native_source("kde"),
                                            foreground=self.foreground)
                self._native[kind] = tuple(self._render_set(svg) for svg in frames)
            return self._native[kind][frame % FRAME_COUNT]
        if kind == "native-gnome-update":
            if kind not in self._native:
                root = ET.fromstring(self._native_source("gnome"))
                self._native[kind] = self._render_set(ET.tostring(
                    _monochrome(root, foreground=self.foreground), encoding="unicode"))
            return self._native[kind]
        if kind.startswith("native-") and kind in NATIVE_PATHS:
            if kind not in self._native:
                if kind.endswith("-failed"):
                    platform = "kde" if "-kde-" in kind else "gnome"
                    svg = _native_failure_svg(
                        self._native_source(platform), self._native_source(kind),
                        kde=platform == "kde", foreground=self.foreground)
                    self._native[kind] = self._render_set(svg)
                else:
                    root = ET.fromstring(self._native_source(kind))
                    svg = ET.tostring(_monochrome(root, foreground=self.foreground),
                                      encoding="unicode")
                    self._native[kind] = self._render_set(svg)
            return self._native[kind]
        if kind in self.theme_paths and kind != "kde":
            if kind not in self._themes:
                self._themes[kind] = self._render_set(self._theme_source(kind))
            return self._themes[kind]
        if kind == "updating":
            if self._running is None:
                source = self._source()
                if self.panel_desktop == "gnome":
                    source = gnome_spinning_svg(source)
                frames = rotated_svg_frames(source)
                self._running = tuple(self._render_set(svg) for svg in frames)
            return self._running[frame % FRAME_COUNT]
        if kind == "kde":
            if kind not in self._themes:
                frames = kde_svg_frames(self._theme_source(kind))
                self._themes[kind] = tuple(self._render_set(svg) for svg in frames)
            return self._themes[kind][frame % FRAME_COUNT]
        if kind == "bouncing-mono":
            if self._mono_bouncing is None:
                frames = bouncing_svg_frames(self._logo_source(kind), monochrome=True,
                                             foreground=self.foreground)
                self._mono_bouncing = tuple(self._render_set(svg) for svg in frames)
            return self._mono_bouncing[frame % FRAME_COUNT]
        if kind == "bouncing":
            if self._bouncing is None:
                frames = bouncing_svg_frames(self._logo_source(kind))
                self._bouncing = tuple(self._render_set(svg) for svg in frames)
            return self._bouncing[frame % FRAME_COUNT]
        if kind == "restart":
            if self._pending is None:
                self._pending = self._render_set(self._logo_source(kind))
            return self._pending
        if kind in ("restart-mono", "failed-mono"):
            if kind not in self._mono_static:
                state = "restart" if kind == "restart-mono" else "failed"
                with self.logo_path.open("rb") as logo_file:
                    source = logo_file.read(MAX_SVG_BYTES + 1)
                if len(source) > MAX_SVG_BYTES:
                    raise ValueError("Bazzite logo SVG exceeds the size limit")
                svg = badged_svg(source, state, monochrome=True,
                                 foreground=self.foreground)
                self._mono_static[kind] = self._render_set(svg)
            return self._mono_static[kind]
        if kind == "failed":
            if self._failed is None:
                self._failed = self._render_set(self._logo_source(kind))
            return self._failed
        raise ValueError("unknown tray artwork state: " + str(kind))
