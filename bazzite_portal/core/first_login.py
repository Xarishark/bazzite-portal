"""Persistent acknowledgement for the packaged app's first graphical login."""
from bazzite_portal.core.once import OnceMarker


class FirstLogin(OnceMarker):
    """Keep first-login state separate from resettable app preferences."""

    def __init__(self, base=None):
        super().__init__("first-login.json", base)
