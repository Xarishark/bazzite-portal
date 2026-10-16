"""Read-only summaries of requested RPM-OSTree customizations."""

MAX_ITEMS = 16
MAX_TEXT = 96


def _text(value):
    if not isinstance(value, str):
        return "Unknown"
    return "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:MAX_TEXT].strip() or "Unknown"


def _packages(item, key):
    values = item.get(key)
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        return "Unknown"
    if not values:
        return "None"
    shown = ", ".join(_text(v) for v in values[:MAX_ITEMS])
    if len(values) > MAX_ITEMS:
        shown += f" (+{len(values) - MAX_ITEMS} more; see full details)"
    return shown


def summarize_customizations(data):
    """Describe current and next-boot requests, never infer missing fields empty."""
    unknown = {"known": False, "rows": [], "error": "Customization details are unavailable."}
    if not isinstance(data, dict):
        return unknown
    entries = data.get("deployments")
    if (not isinstance(entries, list) or not entries or len(entries) > 32
            or any(not isinstance(v, dict) or not isinstance(v.get("booted"), bool) for v in entries)
            or sum(v["booted"] for v in entries) != 1):
        return unknown
    running = next(v for v in entries if v["booted"])
    selected = [("Running now", running)]
    if entries[0] is not running:
        selected.append(("Next restart", entries[0]))
    rows = []
    for role, item in selected:
        scope = f"{role} · {_text(item.get('version'))}"
        rows.append({"title": f"{role} · Package layers", "detail": (
            f"{scope}\nRepository requests: {_packages(item, 'requested-packages')}\n"
            f"Local RPM requests: {_packages(item, 'requested-local-packages')}\n"
            f"File replacement requests: {_packages(item, 'requested-local-fileoverride-packages')}\n"
            f"Applied layers: {_packages(item, 'packages')}"
        )})
        rows.append({"title": f"{role} · Package overrides", "detail": (
            f"{scope}\nRemoval requests: {_packages(item, 'requested-base-removals')}\n"
            f"Local replacement requests: {_packages(item, 'requested-base-local-replacements')}\n"
            f"Repository replacement requests: {_packages(item, 'requested-base-remote-replacements')}"
        )})
        generation = item.get("regenerate-initramfs")
        generation_label = "On" if generation is True else "Off" if generation is False else "Unknown"
        rows.append({"title": f"{role} · Initramfs", "detail": (
            f"{scope}\nLocal generation: {generation_label}\n"
            f"Tracked /etc files: {_packages(item, 'initramfs-etc')}"
        )})
    return {"known": True, "rows": rows, "error": ""}
