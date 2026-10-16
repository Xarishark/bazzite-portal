"""Bounded, identity-checked journal reads for managed maintenance jobs."""

from __future__ import annotations
from bazzite_portal.core.i18n import N_

import json
import re
import unicodedata
from typing import Callable, Any
from bazzite_portal.maintenance.image_switch import INSTANCE_PATTERN as IMAGE_INSTANCE_PATTERN
from bazzite_portal.maintenance.service import valid_unit


_INVOCATION = re.compile(r"^[0-9a-fA-F]{32}$")
_LEGACY_UNITS = (
    re.compile(r"^bazzite-portal-(?:cancel-pending|reset-layers|clear-download-cache|channel-switch|delete-deployment)@[0-9a-f]{64}\.service$"),
    re.compile(r"^bazzite-portal-remove-selected-layers@[0-9a-f]{64}-[0-9a-f]{64}\.service$"),
    re.compile(r"^bazzite-portal-rollback\.service$"),
    re.compile(r"^bazzite-portal-image-switch@" + IMAGE_INSTANCE_PATTERN.pattern.removeprefix("^").removesuffix("$") + r"\.service$"),
)
_MAX_RECORDS = 128
_MAX_MESSAGE_CHARS = 8192
_MAX_TOTAL_BYTES = 32768
_MAX_INPUT_CHARS = 1_048_576


def _clean_message(value: str) -> tuple[str, bool]:
    # Journald messages are displayed as text. Remove terminal, bidi, and other
    # Unicode control/format characters, while retaining ordinary line layout.
    cleaned = "".join(
        char for char in value
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    # Bound both code points and encoded bytes. Keep the tail when a single
    # message is oversized because terminal error details usually appear there.
    limited = len(cleaned) > _MAX_MESSAGE_CHARS
    cleaned = cleaned[-_MAX_MESSAGE_CHARS:]
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_MESSAGE_CHARS:
        limited = True
        encoded = encoded[-_MAX_MESSAGE_CHARS:]
        while encoded and (encoded[0] & 0xC0) == 0x80:
            encoded = encoded[1:]
        cleaned = encoded.decode("utf-8", errors="replace")
    return cleaned, limited


def _message_text(value: Any) -> tuple[str | None, bool]:
    """Decode a journal MESSAGE without allowing a byte array to grow unbounded."""
    if isinstance(value, str):
        return _clean_message(value)
    if isinstance(value, list):
        if any(not isinstance(byte, int) or isinstance(byte, bool) or not 0 <= byte <= 255
               for byte in value[:_MAX_MESSAGE_CHARS]):
            return None, True
        limited = len(value) > _MAX_MESSAGE_CHARS
        raw = bytes(value[:_MAX_MESSAGE_CHARS])
        decoded = raw.decode("utf-8", errors="replace")
        cleaned, cleaned_limited = _clean_message(decoded)
        return cleaned, limited or cleaned_limited
    return None, True


def read_only_valid_unit(unit: str) -> bool:
    """Accept current units and exact legacy names for in-flight upgrade reads."""
    return valid_unit(unit) or (
        isinstance(unit, str) and any(pattern.fullmatch(unit) for pattern in _LEGACY_UNITS)
    )


def read(unit: str, invocation: str, command_runner: Callable[..., str]) -> dict[str, Any]:
    """Return bounded messages for one validated unit invocation.

    ``command_runner`` follows ``system.command``: it accepts an argv list and
    optional timeout, and returns stdout as text. No journal command is run
    unless both identifiers are valid.
    """
    if not read_only_valid_unit(unit):
        return {"logs": [], "error": "Maintenance logs are unavailable for this unit."}
    if not isinstance(invocation, str) or not _INVOCATION.fullmatch(invocation):
        return {"logs": [], "error": "Maintenance logs are unavailable for this invocation."}

    argv = [
        "journalctl", "-b", "_SYSTEMD_UNIT=" + unit, "_SYSTEMD_INVOCATION_ID=" + invocation.lower(),
        "--no-pager", "--quiet", "-n", str(_MAX_RECORDS), "-o", "json",
    ]
    try:
        output = command_runner(argv, timeout=15)
    except Exception as error:
        detail = str(error).strip()
        return {
            "logs": [],
            "error": "Maintenance logs could not be read" + (f": {detail[:512]}" if detail else "."),
        }

    if not isinstance(output, str):
        return {"logs": [], "error": "Maintenance logs returned an invalid response."}

    records = []
    omitted = len(output) > _MAX_INPUT_CHARS
    if omitted:
        # Keep the newest complete records; the first line in this tail may
        # begin partway through a JSON record, so discard it.
        bounded = output[-_MAX_INPUT_CHARS:]
        output = bounded[bounded.find("\n") + 1:] if "\n" in bounded else ""
    try:
        seen_records = 0
        for line in output.splitlines():
            if not line.strip():
                continue
            if seen_records >= _MAX_RECORDS:
                omitted = True
                break
            seen_records += 1
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("journal record is not an object")
            record_unit = entry.get("_SYSTEMD_UNIT")
            record_invocation = entry.get("_SYSTEMD_INVOCATION_ID")
            if (not isinstance(record_unit, str) or record_unit != unit
                    or not isinstance(record_invocation, str)
                    or record_invocation.casefold() != invocation.casefold()):
                raise ValueError("journal record identity did not match")
            cleaned, limited = _message_text(entry.get("MESSAGE"))
            if limited:
                omitted = True
            if cleaned is not None:
                records.append(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"logs": [], "error": "Maintenance logs contained malformed or mismatched journal records."}

    # Keep the newest complete messages. The newline separators are part of
    # the consumer's rendered payload and therefore count against the cap.
    logs = []
    total_bytes = 0
    for message in reversed(records):
        size = len(message.encode("utf-8", errors="replace"))
        separator = 1 if logs else 0
        if total_bytes + separator + size > _MAX_TOTAL_BYTES:
            omitted = True
            break
        logs.append(message)
        total_bytes += separator + size
    logs.reverse()
    return {
        "logs": logs,
        "error": N_("Some operation output was unavailable or omitted.") if omitted else "",
    }
