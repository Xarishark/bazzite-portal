"""Gettext presentation, without localizing parser input or agent state."""
from functools import lru_cache
import gettext as _gettext
import json
import locale
from pathlib import Path
import re
from string import Formatter
import sys

DOMAIN = "bazzite-portal"
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_LOCALE = _ROOT / "build" / "locale"
LOCALE_DIR = (_SOURCE_LOCALE if _SOURCE_LOCALE.is_dir()
              else Path(sys.prefix) / "share" / "locale")
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    # A missing OS locale must not prevent the app from opening.
    pass
_translation = _gettext.translation(DOMAIN, localedir=LOCALE_DIR, fallback=True)
def gettext(message):
    return _translation.gettext(message) if message else message


_ = gettext


def ngettext(singular, plural, count):
    return _translation.ngettext(singular, plural, count)


def pgettext(context, message):
    return _translation.pgettext(context, message)


def N_(message):
    """Mark an English wire/diagnostic value for translation at display time."""
    return message


def C_(message, **values):
    """Format an English wire message; never translate shared backend state."""
    return message.format(**values)


@lru_cache(maxsize=1)
def _message_patterns():
    # Generated from literal C_ calls, shared by the UI and the independent agent.
    path = Path(__file__).with_name("message_templates.json")
    try:
        templates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    result = []
    for template in templates:
        pieces, names = [], []
        for literal, name, spec, conversion in Formatter().parse(template):
            pieces.append(re.escape(literal))
            if name is not None:
                if name in names:
                    pieces.append(f"(?P={name})")
                else:
                    pieces.append(f"(?P<{name}>.*?)")
                    names.append(name)
        result.append((re.compile("".join(pieces), re.DOTALL), template))
    return tuple(result)


def format_message(message):
    """Translate known app messages only; external output remains verbatim.

    Full-message matching avoids translating fragments of logs or commands.
    This is a presentation boundary, never a parser or persisted-state API.
    """
    if not isinstance(message, str) or not message:
        return message
    translated = gettext(message)
    if translated != message:
        return translated
    # With no installed catalog there is nothing to translate or compile.
    if type(_translation) is _gettext.NullTranslations:
        return message
    for pattern, template in _message_patterns():
        match = pattern.fullmatch(message)
        if match is not None:
            translated = gettext(template)
            if translated != template:
                try:
                    return translated.format(**match.groupdict())
                except (KeyError, ValueError, IndexError):
                    # A broken community translation must not break operations.
                    return message
    return message
