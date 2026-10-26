"""Read and change only the UUPD timer on Bazzite Deck images."""
import json
import os
from pathlib import Path
import re
import subprocess

from bazzite_portal.maintenance import runtime

IMAGE_INFO = Path("/usr/share/ublue-os/image-info.json")
TIMER = "uupd.timer"


def status():
    result = {"visible": False, "known": False, "enabled": False, "reason": ""}
    try:
        with IMAGE_INFO.open(encoding="utf-8") as source:
            image = json.loads(source.read(65537))
        name = image.get("image-name")
        if not isinstance(name, str) or not isinstance(image.get("image-vendor"), str):
            raise ValueError("Missing image identity")
    except (OSError, ValueError, AttributeError, RecursionError):
        return {**result, "reason": "Could not verify the installed Bazzite image."}
    result.update(known=True, visible=image["image-vendor"] == "ublue-os"
                  and re.fullmatch(r"bazzite-deck(?:-[a-z0-9]+)*", name) is not None)
    if not result["visible"]:
        return result
    try:
        output = runtime.command(["systemctl", "show", TIMER,
                                  "--property=LoadState,UnitFileState,ActiveState"], timeout=15)
        properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if (properties.get("LoadState") != "loaded"
                or properties.get("UnitFileState") not in {"enabled", "disabled"}
                or properties.get("ActiveState") not in {"active", "inactive"}):
            raise RuntimeError("Unsupported timer state")
        result["enabled"] = properties["UnitFileState"] == "enabled" and properties["ActiveState"] == "active"
    except (OSError, RuntimeError, subprocess.SubprocessError):
        result.update(known=False, reason="Automatic update scheduling is unavailable. Check the UUPD timer configuration.")
    return result


def request(enabled):
    """Ask the allowlisted root service to apply an explicit scheduling choice."""
    if type(enabled) is not bool:
        raise ValueError("Invalid automatic update setting.")
    current = status()
    if not current["visible"] or not current["known"]:
        raise RuntimeError(current["reason"] or "This setting is only available on Bazzite Deck images.")
    action = "enable" if enabled else "disable"
    runtime.command(["systemctl", "--no-ask-password", "start", "--wait",
                     f"bazzite-portal-maintenance@auto-updates-{action}.service"], timeout=45)
    current = status()
    if not current["known"] or current["enabled"] != enabled:
        raise RuntimeError("Could not verify the automatic update setting.")
    return current


def main(argv):
    if len(argv) != 1 or argv[0] not in {"enable", "disable"}:
        raise ValueError("Invalid automatic update operation.")
    if os.geteuid() != 0:
        raise RuntimeError("This helper must run as root.")
    with runtime.maintenance_lock():
        # Re-read trusted image identity and timer state inside the root helper.
        current = status()
        if not current["visible"] or not current["known"]:
            raise RuntimeError(current["reason"] or "This setting is only available on Bazzite Deck images.")
        # Disabling the timer never stops a running uupd.service/manual update.
        runtime.command(["systemctl", argv[0], "--now", TIMER], timeout=30)
    return 0
