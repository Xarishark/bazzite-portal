"""Single SVG layers for GTK transforms; the SNI frame renderer stays separate."""
from copy import deepcopy
import xml.etree.ElementTree as ET
from bazzite_portal.artwork.appearance import artwork_kind
from bazzite_portal.artwork.frames import (
    DEFAULT_SVG,
    DEFAULT_PORTAL,
    KDE_UPDATE,
    GNOME_UPDATE,
    MAX_SVG_BYTES,
    rotated_svg_frames,
    bouncing_svg_frames,
    _native_kde_frames,
    _native_gnome_frames,
)


def _read(path):
    with path.open("rb") as source:
        data = source.read(MAX_SVG_BYTES + 1)
    if len(data) > MAX_SVG_BYTES:
        raise ValueError("Home SVG exceeds size limit")
    return data


def _svg(root):
    return ET.tostring(root, encoding="unicode")


def _only(root, elements):
    result = deepcopy(root)
    for child in list(result):
        if child.tag.split("}")[-1] != "defs":
            result.remove(child)
    result.extend(deepcopy(element) for element in elements)
    return _svg(result)


def layers(theme):
    """Split existing artwork once, retaining its original gradient and cutouts."""
    kind = artwork_kind(theme, "updating")
    if kind == "native-gnome-update":
        root = ET.fromstring(_native_gnome_frames(_read(GNOME_UPDATE), 1)[0])
        body = next(element for element in root if element.tag.split("}")[-1] == "path")
        body.attrib.pop("mask")
        mask = next(element for element in root.iter() if element.get("id") == "tray-motion-mask")
        moving = deepcopy(next(element for element in mask.iter()
                               if element.tag.split("}")[-1] == "g")[0])
        moving.set("fill", "white")
        height = float(root.get("viewBox").split()[3])
        return {"base": _only(root, [body]), "moving": _only(root, [moving]),
                "masked": True, "motion": "bounce", "amplitude": .5 / height}
    if kind == "native-kde-update":
        root = ET.fromstring(_native_kde_frames(_read(KDE_UPDATE), 1)[0])
        group = next(element for element in root if element.tag.split("}")[-1] == "g")
        root.remove(group)
        x, y, width, height = map(float, root.get("viewBox").split())
        return {"base": _svg(root), "moving": _only(root, [group]),
                "motion": "rotate", "center": ((16 - x) / width, (16 - y) / height)}
    bounce, mono = kind.startswith("bouncing"), kind.endswith("-mono")
    source = _read(DEFAULT_PORTAL if bounce else DEFAULT_SVG)
    svg = (bouncing_svg_frames(source, 1, mono)[0] if bounce else rotated_svg_frames(source, 1, mono)[0])
    root = ET.fromstring(svg)
    x, y, width, height = map(float, root.get("viewBox").split())
    path_id = "path4" if bounce else "path3"
    path = next(element for element in root if element.get("id") == path_id)
    underlay = deepcopy(root)
    underlay.remove(next(element for element in underlay if element.get("id") == path_id))
    outer = deepcopy(path)
    outer.attrib.pop("mask", None)
    mask = next(element for element in root.iter() if element.get("id") == "tray-motion-mask")
    moving_group = next(element for element in mask if element.tag.split("}")[-1] == "g")
    moving = deepcopy(list(moving_group)[0])
    moving.set("fill", "white")
    fixed = [deepcopy(element) for element in mask
             if element.tag.split("}")[-1] == "path" and element.get("fill") == "black"]
    for element in fixed:
        element.set("fill", "white")
    return {"underlay": _svg(underlay), "base": _only(root, [outer]),
            "fixed_mask": _only(root, fixed), "moving": _only(root, [moving]),
            "masked": True, "motion": "bounce" if bounce else "rotate",
            "center": ((8.5 - x) / width, (10.283 - y) / height), "amplitude": .5 / height}
