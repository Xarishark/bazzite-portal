"""Image-owned YAML and stable action references, independent of GTK."""
import hashlib
import json
import logging
import subprocess

log = logging.getLogger(__name__)

DEFAULT_CONFIG = "/usr/share/bazzite-portal/tools.yml"


def load_config(path=DEFAULT_CONFIG):
    import yaml
    with open(path, encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict) or not isinstance(data.get("screens", []), list):
        raise ValueError("Portal configuration must contain a list of screens.")
    result = []
    for screen in data.get("screens", []):
        if not isinstance(screen, dict) or not isinstance(screen.get("actions", []), list):
            raise ValueError("Portal category has an invalid action list.")
        category = str(screen.get("title", "Tools"))
        for action in screen.get("actions", []):
            if not isinstance(action, dict):
                raise ValueError("Portal action is not a mapping.")
            for key in ("script", "status_script", "title", "description", "id"):
                if action.get(key) is not None and not isinstance(action[key], str):
                    raise ValueError(f"Portal action {key} must be text.")
            options = action.get("options") or []
            if not isinstance(options, list) or any(not isinstance(o, dict) or any(o.get(k) is not None and not isinstance(o[k], str) for k in ("id", "label", "script")) for o in options):
                raise ValueError("Portal action has invalid options.")
            result.append({"category": category, "action": action})
    return result


def reference(item):
    action = item["action"]
    if action.get("id"):
        return {"category": item["category"], "id": action["id"]}
    signature = hashlib.sha256(json.dumps({k: action.get(k) for k in ("script", "status_script", "options")}, sort_keys=True).encode()).hexdigest()
    return {"category": item["category"], "title": action.get("title", "Action"), "signature": signature}


def resolve(items, pin):
    matches = [item for item in items if reference(item) == pin]
    return matches[0] if len(matches) == 1 else None


def search(items, query):
    words = query.casefold().split()
    return [item for item in items if all(word in (item["category"] + " " + item["action"].get("title", "") + " " + item["action"].get("description", "")).casefold() for word in words)]


def terminal(script):
    from bazzite_portal import APP_ID
    if not script:
        raise ValueError("No command is configured.")
    # Scripts come only from the image-owned YAML, never from stored pins.
    return subprocess.Popen(["xdg-terminal-exec", "--app-id=" + APP_ID, "--title=Bazzite Portal", "--", "bash", "--noprofile", "--norc", "-lc", script])


def status(script, *, tool=None):
    if not script:
        return ""
    try:
        result = subprocess.run(["bash", "--noprofile", "--norc", "-lc", script], capture_output=True, text=True, timeout=3)
        return next((line.strip() for line in result.stdout.splitlines() if line.strip()), "") if result.returncode == 0 else ""
    except subprocess.TimeoutExpired as error:
        log.warning("Tool status check timed out after %ss (tool=%r)", error.timeout, tool or "unspecified")
        return ""
    except OSError:
        return ""
