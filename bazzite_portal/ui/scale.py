"""GTK content scaling that keeps measurement and input coordinates aligned."""
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gsk, Gtk


class ScaledContent(Gtk.Widget):
    """A single child rendered and measured at an application-local scale.

    The child is allocated in its original coordinate space and the allocation
    transform scales both rendering and event coordinates. Its natural size is
    scaled during measurement so parents reserve the corresponding space.
    """

    def __init__(self, child, factor=1.0):
        super().__init__()
        self.factor = float(factor)
        if not math.isfinite(self.factor) or self.factor <= 0:
            raise ValueError("scale factor must be a positive finite number")
        self._child = None
        self.set_child(child)

    def set_child(self, child):
        if child is self._child:
            return
        if child is not None and child.get_parent() is not None:
            raise ValueError("scaled content child already has a parent")
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)
        self.queue_resize()

    def get_child(self):
        return self._child

    def detach(self):
        """Unparent and return the child, for explicit window cleanup."""
        child, self._child = self._child, None
        if child is not None:
            child.unparent()
            self.queue_resize()
        return child

    def do_measure(self, orientation, for_size):
        child = self._child
        if child is None or not child.get_visible():
            return 0, 0, -1, -1
        child_for_size = -1 if for_size < 0 else math.floor(for_size / self.factor)
        minimum, natural, min_baseline, nat_baseline = child.measure(
            orientation, child_for_size)
        return (math.ceil(minimum * self.factor),
                math.ceil(natural * self.factor),
                -1 if min_baseline < 0 else math.ceil(min_baseline * self.factor),
                -1 if nat_baseline < 0 else math.ceil(nat_baseline * self.factor))

    def do_get_request_mode(self):
        return self._child.get_request_mode() if self._child is not None else Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_size_allocate(self, width, height, baseline):
        child = self._child
        if child is None or not child.get_visible():
            return
        child_width = max(0, math.floor(width / self.factor))
        child_height = max(0, math.floor(height / self.factor))
        child_baseline = -1 if baseline < 0 else math.floor(baseline / self.factor)
        transform = Gsk.Transform.new().scale(self.factor, self.factor)
        child.allocate(child_width, child_height, child_baseline, transform)

    def do_snapshot(self, snapshot):
        if self._child is not None and self._child.get_visible():
            self.snapshot_child(self._child, snapshot)

def scaled_content(child, factor=1.0):
    """Wrap ``child`` in a measured and input-aware scaling widget."""
    if float(factor) == 1.0:
        return child
    return ScaledContent(child, factor)
