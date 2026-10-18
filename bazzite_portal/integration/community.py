"""Community links and a bounded reader for the Bazzite announcements feed."""

from __future__ import annotations

import ipaddress
import re
import urllib.request
import xml.etree.ElementTree as ET
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

# Keep the established feed endpoint until its live replacement is verified.
NEWS_FEED = "https://universal-blue.discourse.group/tag/bazzite-news/11.rss"
MAX_FEED_BYTES = 1024 * 1024
MAX_ANNOUNCEMENTS = 20
MAX_TITLE_LENGTH = 256
MAX_DATE_LENGTH = 128


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


def parse_announcements(payload: bytes) -> list[dict[str, str]]:
    """Parse safe RSS entries, bounded by input size, item count, and field size."""
    if not isinstance(payload, bytes):
        raise ValueError("announcement feed payload must be bytes")
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("announcement feed exceeds the size limit")
    # ElementTree does not resolve external entities, but refusing declarations
    # also prevents entity expansion from turning a small document into a large one.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, flags=re.IGNORECASE):
        raise ValueError("announcement feed contains a forbidden XML declaration")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError("announcement feed is not valid XML") from exc

    local_name = lambda tag: tag.rsplit("}", 1)[-1].lower()
    if local_name(root.tag) != "rss":
        raise ValueError("announcement feed is not RSS")
    channel = next((child for child in root if local_name(child.tag) == "channel"), None)
    if channel is None:
        raise ValueError("RSS announcement feed has no channel")

    results: list[dict[str, str]] = []
    for item in channel:
        if local_name(item.tag) != "item":
            continue
        fields = {local_name(child.tag): (child.text or "").strip() for child in item}
        title = fields.get("title", "")[:MAX_TITLE_LENGTH].strip()
        url = fields.get("link", "").strip()
        date = fields.get("pubdate", fields.get("published", fields.get("updated", "")))[:MAX_DATE_LENGTH].strip()
        if not title or not valid_link(url):
            continue
        results.append({"title": title, "url": url, "date": date})
        if len(results) >= MAX_ANNOUNCEMENTS:
            break
    return results


def fetch_announcements(version: str) -> list[dict[str, str]]:
    """Fetch and parse the community feed with a timeout and strict byte limit."""
    safe_version = "".join(char for char in str(version) if 32 <= ord(char) < 127)[:64]
    request = urllib.request.Request(NEWS_FEED, headers={"User-Agent": f"Bazzite-App/{safe_version}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    return parse_announcements(payload)
