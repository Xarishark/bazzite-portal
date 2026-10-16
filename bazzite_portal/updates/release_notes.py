"""Bounded same-release GitHub notes lookup, including image build revisions."""
from __future__ import annotations
import json
import re
from typing import Any, Callable
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bazzite_portal.core.i18n import N_

MAX_ERROR_LENGTH = 1200
RELEASE_TIMEOUT_SECONDS = 8
MAX_RELEASE_BYTES = 1024 * 1024
MAX_RELEASE_PAGES = 3
_RELEASE_API = "https://api.github.com/repos/ublue-os/bazzite/releases?per_page=30&page={}"
_RELEASE_WEB_PREFIX = "https://github.com/ublue-os/bazzite/releases/tag/"
_VERSION_RE = re.compile(r"^(?:(?:testing|stable)-)?v?(\d{2,4}\.\d{8}(?:\.\d+)?)$", re.IGNORECASE)

def _clean(value: Any, limit: int = MAX_ERROR_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()
    return cleaned.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _release_body(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127)).strip()
    return cleaned.encode("utf-8")[:64 * 1024].decode("utf-8", errors="ignore")


def _release_version(release: dict[str, Any]) -> str | None:
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        return None
    match = _VERSION_RE.fullmatch(tag)
    if match:
        return match.group(1)
    # Testing release tags are prefixed with the channel.
    match = re.fullmatch(r"(?:testing|stable)-(\d{2,4}\.\d{8}(?:\.\d+)?)", tag, re.IGNORECASE)
    return match.group(1) if match else None


def _release_matches(release: Any, version: str, channel: str) -> bool:
    if not isinstance(release, dict) or release.get("draft") is True:
        return False
    # GitHub marks Bazzite testing releases as prereleases; they are valid matches.
    actual = _release_version(release)
    # Image rebuilds (.0, .1, ...) share the base date release notes. Keep the
    # deployment identity unchanged; normalize only this lookup comparison.
    base_version = ".".join(version.split(".")[:2])
    if actual not in (version, base_version):
        return False
    tag = release.get("tag_name", "")
    name = release.get("name", "")
    text = " ".join(part for part in (tag, name) if isinstance(part, str)).casefold()
    if channel == "testing":
        return "testing" in text
    return "stable" in text or tag.casefold().startswith("stable-")


def _read_release_page(opener: Callable[..., Any], page: int) -> list[dict[str, Any]]:
    request = Request(
        _RELEASE_API.format(page),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Bazzite-App"},
    )
    with opener(request, timeout=RELEASE_TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_RELEASE_BYTES + 1)
    if len(raw) > MAX_RELEASE_BYTES:
        raise ValueError("GitHub release response exceeded the size limit")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("GitHub release response was not a list")
    return [item for item in data if isinstance(item, dict)]


def fetch_release_notes(
    version: str,
    channel: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, str]:
    """Find Bazzite release notes for a known date version and channel.

    Image build revisions may use their base date release. This optional lookup
    never falls back to another date or channel, or to the latest release.
    """
    version_match = _VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    normalized_channel = channel.casefold() if isinstance(channel, str) else ""
    if version_match is None or normalized_channel not in {"stable", "testing"}:
        return {"state": "unknown", "url": "", "title": "", "body": "", "error": N_("Version or release channel is unknown.")}
    normalized_version = version_match.group(1)
    try:
        for page in range(1, MAX_RELEASE_PAGES + 1):
            releases = _read_release_page(opener, page)
            for release in releases:
                if _release_matches(release, normalized_version, normalized_channel):
                    url = release.get("html_url")
                    title = _clean(release.get("name", ""), 256)
                    body = release.get("body", "")
                    tag = release.get("tag_name", "")
                    expected_url = _RELEASE_WEB_PREFIX + quote(tag, safe="") if isinstance(tag, str) else ""
                    if not isinstance(url, str) or url != expected_url:
                        continue
                    return {"state": "available", "url": url, "title": title, "body": _release_body(body), "error": ""}
            if len(releases) < 30:
                break
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"state": "unknown", "url": "", "title": "", "body": "", "error": "Release notes lookup failed: {error}".format(error=_clean(str(exc)))}
    return {"state": "missing", "url": "", "title": "", "body": "", "error": N_("No release notes match this version and channel.")}
