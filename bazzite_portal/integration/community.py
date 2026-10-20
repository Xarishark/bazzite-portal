"""Community links and HTTPS destination validation."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


from bazzite_portal.core.i18n import C_, N_

LINKS = (
    ("website", C_('{bazzite} website', bazzite='Bazzite'), C_('The {bazzite} project website', bazzite='Bazzite'), "https://bazzite.gg/"),
    ("docs", N_("Documentations"), C_('{bazzite} documentation', bazzite='Bazzite'), "https://docs.bazzite.gg/"),
    ("discord", "Discord", C_('{bazzite} community and support', bazzite='Bazzite'), "https://discord.bazzite.gg/"),
    ("github", "GitHub", C_('{bazzite} source repository', bazzite='Bazzite'), "https://github.com/ublue-os/bazzite"),
    ("reddit", "Reddit", C_('The {bazzite} subreddit', bazzite='Bazzite'), "https://www.reddit.com/r/Bazzite/"),
    ("forums", N_("Forums"), C_('{universal_blue} community forums', universal_blue='Universal Blue'), "https://universal-blue.discourse.group/c/bazzite/5"),
)


def valid_link(url: str) -> bool:
    """Return whether *url* is a well-formed HTTPS URL with a real host."""
    if not isinstance(url, str) or not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            return False
        hostname = parsed.hostname
        if not hostname or any(char.isspace() for char in hostname):
            return False
        # Accessing port validates its numeric syntax and range.
        _ = parsed.port
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            ascii_host = hostname.rstrip(".").encode("idna").decode("ascii")
            if not ascii_host or len(ascii_host) > 253:
                return False
            labels = ascii_host.split(".")
            if any(not label or len(label) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) for label in labels):
                return False
        return True
    except (UnicodeError, ValueError):
        return False
