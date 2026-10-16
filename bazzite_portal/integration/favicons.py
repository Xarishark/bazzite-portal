"""Best-effort, bounded runtime loading of the community sites' favicons."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import urllib.request
import uuid
import urllib.error


SOURCES = {
    "docs": "https://docs.bazzite.gg/static/favicon.svg",
    "website": "https://bazzite.gg/content/uploads/2024/02/icon.svg",
    # Keep GitHub's existing URL so users retain its current runtime cache entry.
    "github": "https://github.githubassets.com/favicons/favicon.svg",
    # Discord mark from the Homarr dashboard-icons SVG collection.
    "discord": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/discord.svg",
    "announcements": "https://global.discourse-cdn.com/free1/uploads/univeral_blue/optimized/2X/e/e39f7d1952979cf23b0f8d634e025fb38da50026_2_32x32.png",
    "forums": "https://global.discourse-cdn.com/free1/uploads/univeral_blue/optimized/2X/e/e39f7d1952979cf23b0f8d634e025fb38da50026_2_32x32.png",
    # Reddit mark from the Homarr dashboard-icons SVG collection.
    "reddit": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/reddit.svg",
}

MAX_FAVICON_BYTES = 256 * 1024
FETCH_TIMEOUT_SECONDS = 10


def _default_cache_root() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    return (Path(cache_home) if cache_home else Path.home() / ".cache") / "bazzite-portal" / "favicons"


def _extension(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".png", ".ico", ".svg"} else ".img"


def _cache_path(url: str, cache_root: Path) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_root / f"{digest}{_extension(url)}"


def _valid_image(data: bytes) -> bool:
    if not isinstance(data, bytes) or not data or len(data) > MAX_FAVICON_BYTES:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\x00\x00\x01\x00"):
        return True
    # Permit an XML declaration or comments before an SVG root, but not HTML.
    prefix = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\n\r ")
    if prefix.startswith(b"<?xml"):
        declaration_end = prefix.find(b"?>")
        if declaration_end < 0:
            return False
        prefix = prefix[declaration_end + 2:].lstrip()
    while prefix.startswith(b"<!--"):
        comment_end = prefix.find(b"-->")
        if comment_end < 0:
            return False
        prefix = prefix[comment_end + 3:].lstrip()
    return prefix.startswith((b"<svg ", b"<svg\n", b"<svg\r", b"<svg\t", b"<svg>"))


def _https_url(url: str) -> bool:
    # Redirect targets must remain HTTPS. Reject credentials and control chars.
    from urllib.parse import urlsplit

    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if not _https_url(new_url):
            raise urllib.error.HTTPError(new_url, code, "Refusing non-HTTPS favicon redirect", headers, file_pointer)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def cached_favicons(cache_root: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    """Return paths for bounded, recognizable favicon files already in cache."""
    root = Path(cache_root) if cache_root is not None else _default_cache_root()
    found: dict[str, Path] = {}
    for site_id, url in SOURCES.items():
        path = _cache_path(url, root)
        try:
            if path.is_file() and path.stat().st_size <= MAX_FAVICON_BYTES:
                data = path.read_bytes()
                if _valid_image(data):
                    found[site_id] = path
        except OSError:
            continue
    return found


def fetch_favicons(
    version: str,
    cache_root: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Fetch missing favicons into a runtime cache, returning successful paths."""
    safe_version = "".join(char for char in str(version) if 32 <= ord(char) < 127)[:64]
    root = Path(cache_root) if cache_root is not None else _default_cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return cached_favicons(root)

    results = cached_favicons(root)
    paths_by_url = {_cache_path(url, root): url for url in SOURCES.values()}
    completed_urls = {SOURCES[site_id] for site_id in results}
    opener = urllib.request.build_opener(_HTTPSRedirectHandler)
    for path, url in paths_by_url.items():
        if url in completed_urls:
            continue
        completed_urls.add(url)
        if not _https_url(url):
            continue
        request = urllib.request.Request(url, headers={"User-Agent": f"Bazzite-App/{safe_version}"})
        try:
            with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                if not _https_url(final_url):
                    continue
                data = response.read(MAX_FAVICON_BYTES + 1)
            if not _valid_image(data):
                continue
            temp_path = path.with_name(f".favicon-{uuid.uuid4().hex}.tmp")
            try:
                with temp_path.open("xb") as temp:
                    temp.write(data)
                os.replace(temp_path, path)
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            for site_id, site_url in SOURCES.items():
                if site_url == url:
                    results[site_id] = path
        except (OSError, ValueError):
            continue
    return results
