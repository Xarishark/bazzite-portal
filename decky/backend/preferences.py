"""Plugin-only preferences, kept outside Loader's replaceable plugin folder."""
import json
import os
from pathlib import Path


def read():
    path = Path(os.environ["DECKY_PLUGIN_SETTINGS_DIR"]) / "preferences.json"
    try:
        value = json.loads(path.read_text())
        return {"show_logs": value.get("show_logs") is True}
    except (OSError, ValueError, AttributeError):
        return {"show_logs": False}


def set_show_logs(enabled):
    if type(enabled) is not bool:
        raise ValueError("Invalid log visibility preference")
    path = Path(os.environ["DECKY_PLUGIN_SETTINGS_DIR"]) / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"show_logs": enabled}))
    temporary.replace(path)
    return {"show_logs": enabled}
