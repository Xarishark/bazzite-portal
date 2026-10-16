"""Keep internal diagnostics in English logs and actionable messages in the UI."""
from collections import OrderedDict
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
import threading

from .i18n import N_, _

log = logging.getLogger(__name__)
_reported = OrderedDict()
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _messages():
    try:
        values = json.loads(Path(__file__).with_name("user_messages.json").read_text(encoding="utf-8"))
        return frozenset(values) | frozenset(_(value) for value in values)
    except (OSError, ValueError, TypeError):
        # A missing resource must never cause raw diagnostic text to escape.
        return frozenset()


def public_error(error, fallback, *, context="Application"):
    """Preserve recognized user guidance; log unfamiliar errors once, bounded.

    Only complete, literal UI messages are accepted. Backend details, command
    output and interpolated exception wrappers cannot bypass this boundary.
    The original operation log and backend state are never rewritten.
    """
    detail = str(error or "").strip()
    if not detail:
        return _(fallback)
    wrapper = re.fullmatch(r"(?:g-io-error-quark: )?GDBus\.Error:[\w.]+: (.*?)(?: \([0-9]+\))?", detail, re.DOTALL)
    candidate = wrapper[1] if wrapper else detail
    if candidate in _messages():
        return _(candidate)
    key = (context, detail[:4096])
    with _lock:
        report = key not in _reported
        _reported[key] = None
        _reported.move_to_end(key)
        while len(_reported) > 256:
            _reported.popitem(last=False)
    if report:
        log.warning("%s: %s", context, key[1])
    if candidate.startswith("Another system operation is running: "):
        return _("Another system operation is running. Wait for it to finish.")
    if candidate in {"Access denied", "Permission denied", "Not authorized"}:
        return _("Authorization was not granted. Try again or cancel.")
    return _(fallback)


def error_text(error, fallback=N_("Details are unavailable. Open Logs for more information."), *, context="Application"):
    """Filter an optional status error without turning empty success into error."""
    return public_error(error, fallback, context=context) if error else error
