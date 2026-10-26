"""Bounded notification receipts outside the replaceable plugin folder."""
import json
import os
from pathlib import Path
import re

RUN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def claim(run):
    if not isinstance(run, str) or not RUN_PATTERN.fullmatch(run):
        return False
    path = Path(os.environ["DECKY_PLUGIN_SETTINGS_DIR"]) / "notified-updates.json"
    try:
        with path.open(encoding="utf-8") as source:
            previous = json.loads(source.read(4097))
        if (not isinstance(previous, list) or len(previous) > 32
                or any(not isinstance(item, str) or not RUN_PATTERN.fullmatch(item) for item in previous)):
            previous = []
    except (OSError, ValueError, RecursionError):
        previous = []
    if run in previous:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps((previous + [run])[-32:]), encoding="utf-8")
    temporary.replace(path)
    return True
