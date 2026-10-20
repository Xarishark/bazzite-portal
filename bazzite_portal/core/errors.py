"""Keep internal diagnostics in English logs and actionable messages in the UI."""
from collections import OrderedDict
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
import threading

from .i18n import N_, _, format_message

log = logging.getLogger(__name__)
_reported = OrderedDict()
_lock = threading.Lock()
_SPACE_ERROR = re.compile(
    # Native ENOSPC, bootc preflight, and libostree reserve/delta failures.
    r"\b(?:no space left on device|ENOSPC|insufficient free space for)\b"
    r"|\bmin-free-space-(?:percent '[0-9]+%'|size [0-9]+MB) would be exceeded\b"
    r"|\bDelta requires [^\n]+ free space, but only [^\n]+ available\b",
    re.IGNORECASE,
)
_CONNECTION_ERROR = re.compile(
    r"\b(?:could not resolve (?:host|hostname)|name or service not known|"
    r"temporary failure in name resolution|no route to host|host is unreachable|"
    r"network is unreachable|connection refused|connection reset by peer|"
    r"connection was closed|connection closed unexpectedly|could not connect to server)\b",
    re.IGNORECASE,
)
_SERVER_BUSY = re.compile(
    r"\b(?:Server returned HTTP\s+(?:429|500|503)|"
    r"Server returned status\s+(?:429|500|503))\b",
    re.IGNORECASE,
)
_REPOSITORY_METADATA_UNAVAILABLE = re.compile(
    r"\b(?:Cannot download repomd\.xml: .*All mirrors were tried|"
    r"Cannot download [^\n]+: All mirrors were tried|"
    r"No more mirrors to try - All mirrors were already tried without success|"
    r"Cannot download, all mirrors were already tried without success)\b",
    re.IGNORECASE,
)
_LAYER_CONFLICT = re.compile(
    r"\b(?:nothing provides .+ needed by .+|cannot install both .+ and .+|"
    r"package .+ conflicts with .+ provided by .+|"
    r"package .+ requires .+, but none of the providers can be installed)\b",
    re.IGNORECASE,
)
_CHECKSUM_FAILURE = re.compile(
    r"\b(?:corrupted (?:metadata )?(?:object|file); checksum expected=['\"]?|"
    r"checksum mismatch [^\n]+|"
    r"zchunk header checksum didn't match expected checksum)\b",
    re.IGNORECASE,
)
_SIGNATURE_FAILURE = re.compile(
    r"\b(?:Can't check signature: public key not found|BAD signature|Key revoked|Expired signature)\b",
    re.IGNORECASE,
)


def _error_category(detail, logs):
    """Map only explicit native failure forms to safe, actionable UI text."""
    evidence = [detail]
    evidence.extend(line for line in (logs or ()) if isinstance(line, str))
    joined = "\n".join(evidence)
    if _SPACE_ERROR.search(joined):
        # xgettext:no-python-format
        return _("Not enough free space. Free up space and try again. System updates need at least 3% free space on the installation drive.")
    if _SIGNATURE_FAILURE.search(joined):
        return _("The download's signature could not be verified. Try again later. If the problem continues, open Logs for details.")
    if _CHECKSUM_FAILURE.search(joined):
        return _("A downloaded file failed checksum verification. Try again later and check the operation log if the problem continues.")
    if _LAYER_CONFLICT.search(joined):
        return _("A package conflict is blocking this system change. Review your layered apps in System Management, then try again.")
    if _SERVER_BUSY.search(joined):
        return _("The download server is busy. Wait a little and try again.")
    if _CONNECTION_ERROR.search(joined):
        return _("Could not connect to the download server. Check your connection and try again.")
    if _REPOSITORY_METADATA_UNAVAILABLE.search(joined):
        return _("Could not download software repository information. Try again later. If this continues, check your repository settings or open Logs.")
    return None


@lru_cache(maxsize=1)
def _messages():
    try:
        values = json.loads(Path(__file__).with_name("user_messages.json").read_text(encoding="utf-8"))
        return frozenset(values) | frozenset(format_message(value) for value in values)
    except (OSError, ValueError, TypeError):
        # A missing resource must never cause raw diagnostic text to escape.
        return frozenset()


def public_error(error, fallback, *, context="Application", logs=()):
    """Preserve recognized user guidance; log unfamiliar errors once, bounded.

    Only complete, literal UI messages or recognized failure categories are
    accepted. Operation logs may identify a category, but are never displayed.
    The original operation log and backend state are never rewritten.
    """
    detail = str(error or "").strip()
    if not detail and not logs:
        return format_message(fallback)
    wrapper = re.fullmatch(r"(?:g-io-error-quark: )?GDBus\.Error:[\w.]+: (.*?)(?: \([0-9]+\))?", detail, re.DOTALL)
    candidate = wrapper[1] if wrapper else detail
    category = _error_category(detail, logs)
    if not detail and category is None:
        return format_message(fallback)
    if candidate in _messages() and category is None:
        return format_message(candidate)
    key = (context, detail[:4096])
    with _lock:
        report = key not in _reported
        _reported[key] = None
        _reported.move_to_end(key)
        while len(_reported) > 256:
            _reported.popitem(last=False)
    if report:
        log.warning("%s: %s", context, key[1])
    if category is not None:
        return format_message(category)
    if candidate.startswith("Another system operation is running: "):
        return _("Another system operation is running. Wait for it to finish.")
    if candidate in {"Access denied", "Permission denied", "Not authorized"}:
        return _("Authorization was not granted. Try again or cancel.")
    return format_message(fallback)


def error_text(error, fallback=N_("Details are unavailable. Open Logs for more information."), *, context="Application", logs=()):
    """Filter an optional status error without turning empty success into error."""
    return public_error(error, fallback, context=context, logs=logs) if error or logs else error
