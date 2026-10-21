"""Read the image-owned, manually curated Bazzite announcements file."""

from __future__ import annotations

from datetime import date
import re
from pathlib import Path
from urllib.parse import urlsplit

MAX_FILE_BYTES = 256 * 1024
MAX_TITLE_LENGTH = 256
MAX_SUBTITLE_LENGTH = 512
MAX_LINK_LENGTH = 2048
_DATE_RE = re.compile(r"\d{2}-\d{2}-\d{4}\Z")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class AnnouncementLoadError(ValueError):
    """A safe, user-facing explanation of why announcements could not load."""


def source_path(config_path: str | Path) -> Path:
    """Read announcements beside the active tools configuration."""
    return Path(config_path).with_name("announcements.yml")


def _valid_https_link(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_LINK_LENGTH:
        return False
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate port syntax without restricting valid HTTPS links.
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def load_announcements(path: str | Path) -> list[dict[str, object]]:
    """Load a bounded YAML list, preserving its authored order.

    Each result contains ``title`` and ``subtitle`` strings, a parsed
    ``datetime.date`` under ``date``, and the HTTPS ``link`` string. Invalid
    files raise :class:`AnnouncementLoadError` with a concise display-safe
    message; callers should show that message without aborting the app.
    """
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise AnnouncementLoadError("Announcements are unavailable right now.") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise AnnouncementLoadError("The announcements file is too large to load.")
    import yaml

    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AnnouncementLoadError("The announcements file could not be read.") from exc
    if not isinstance(document, list):
        raise AnnouncementLoadError("The announcements file must contain a YAML list.")
    result: list[dict[str, object]] = []
    for index, item in enumerate(document, start=1):
        if not isinstance(item, dict) or set(item) - {"title", "date", "link", "subtitle"}:
            raise AnnouncementLoadError(f"Announcement {index} has an invalid structure.")
        title = item.get("title")
        date_text = item.get("date")
        link = item.get("link")
        subtitle = item.get("subtitle", "")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
            raise AnnouncementLoadError(f"Announcement {index} needs a short title.")
        if not isinstance(date_text, str) or not _DATE_RE.fullmatch(date_text):
            raise AnnouncementLoadError(f"Announcement {index} needs a date in DD-MM-YYYY format.")
        try:
            parsed_date = date(int(date_text[6:]), int(date_text[3:5]), int(date_text[:2]))
        except ValueError as exc:
            raise AnnouncementLoadError(f"Announcement {index} has an invalid calendar date.") from exc
        if not _valid_https_link(link):
            raise AnnouncementLoadError(f"Announcement {index} needs a valid HTTPS link.")
        if not isinstance(subtitle, str) or len(subtitle) > MAX_SUBTITLE_LENGTH:
            raise AnnouncementLoadError(f"Announcement {index} has an invalid subtitle.")
        result.append({
            "title": title.strip(),
            "date": parsed_date,
            "link": link,
            "subtitle": subtitle.strip(),
        })
    return result


def format_date(value: date, today: date) -> str:
    """Format as ``DD MON`` and append the year when strictly over one year old."""
    if not isinstance(value, date) or not isinstance(today, date):
        raise TypeError("value and today must be dates")
    # Compare with the anniversary one calendar year before today.
    try:
        cutoff = today.replace(year=today.year - 1)
    except ValueError:
        cutoff = today.replace(year=today.year - 1, day=28)
    older_than_one_year = value < cutoff
    label = f"{value.day:02d} {_MONTHS[value.month - 1]}"
    if older_than_one_year:
        label += f" {value.year:04d}"
    return label
