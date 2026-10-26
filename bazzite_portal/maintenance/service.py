"""Strict dispatcher for the root maintenance systemd template."""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys

from bazzite_portal.maintenance import runtime
from bazzite_portal.maintenance.status import read_status


UNIT_PREFIX = "bazzite-portal-maintenance@"
UNIT_SUFFIX = ".service"
_HASH = r"[0-9a-f]{64}"
_SIMPLE_INSTANCES = {
    "auto-updates": re.compile(r"^(enable|disable)$"),
    "cancel-rollback": re.compile(r"^" + _HASH + r"$"),
    "cancel-pending": re.compile(r"^" + _HASH + r"$"),
    "reset-layers": re.compile(r"^" + _HASH + r"$"),
    "remove-selected-layers": re.compile(r"^" + _HASH + r"-" + _HASH + r"$"),
    "clear-download-cache": re.compile(r"^" + _HASH + r"$"),
    "channel-switch": re.compile(r"^" + _HASH + r"$"),
    "delete-deployment": re.compile(r"^" + _HASH + r"$"),
}
_MODULES = {
    "auto-updates": ("auto_updates", "one"),
    "cancel-pending": ("cancel_pending", "one"),
    "cancel-rollback": ("rollback_cancel", "one"),
    "reset-layers": ("reset_layers", "one"),
    "remove-selected-layers": ("selected_layers", "one"),
    "clear-download-cache": ("cache_clear", "one"),
    "channel-switch": ("channel_switch", "one"),
    "delete-deployment": ("delete_deployments", "one"),
    "image-switch": ("image_switch", "one"),
}


def parse_instance(instance: str) -> tuple[str, str] | None:
    """Return an allowlisted operation and opaque validated payload."""
    if not isinstance(instance, str) or len(instance) > 255:
        return None
    if instance == "rollback":
        return "rollback", ""

    for operation in (*_SIMPLE_INSTANCES, "image-switch"):
        prefix = operation + "-"
        if not instance.startswith(prefix):
            continue
        payload = instance[len(prefix):]
        if operation in _SIMPLE_INSTANCES:
            return (operation, payload) if _SIMPLE_INSTANCES[operation].fullmatch(payload) else None
        if operation == "image-switch":
            # Import only the parser constants; this does not execute any helper.
            from bazzite_portal.maintenance.image_switch import INSTANCE_PATTERN, MAX_TAG_BYTES

            match = INSTANCE_PATTERN.fullmatch(payload)
            if (match and payload.isascii() and len(match.group(3)) <= MAX_TAG_BYTES
                    and len(payload) <= MAX_TAG_BYTES + 64 + 2 + 40):
                return "image-switch", payload
        return None
    return None


def valid_unit(unit: str) -> bool:
    """Check a complete unit name against the single supported template."""
    if (not isinstance(unit, str) or len(unit) > 255
            or not unit.startswith(UNIT_PREFIX) or not unit.endswith(UNIT_SUFFIX)):
        return False
    instance = unit[len(UNIT_PREFIX):-len(UNIT_SUFFIX)]
    return parse_instance(instance) is not None


def _rollback() -> int:
    """Run rollback only after the shared lock and fresh busy checks pass."""
    with runtime.maintenance_lock():
        status = read_status(runtime.command)
        if runtime.has_transaction(status.get("transaction")):
            raise RuntimeError("Refusing rollback while an RPM-OSTree transaction is active.")
        # The unified rollback instance is necessarily active while this code
        # runs. Check external work and the legacy rollback unit explicitly;
        # all new app maintenance instances coordinate through the shared lock.
        active = _rollback_busy_operation(runtime.command)
        if active:
            raise RuntimeError(f"Refusing rollback while {active} is running.")

        # Recheck directly before invoking the helper in case an external
        # rpm-ostree transaction began while the systemd state was queried.
        status = read_status(runtime.command)
        if runtime.has_transaction(status.get("transaction")):
            raise RuntimeError("Refusing rollback while an RPM-OSTree transaction is active.")

        environment = {**os.environ, "LC_ALL": "C"}
        result = subprocess.run(
            ["/usr/bin/bazzite-rollback-helper", "rollback", "-y"],
            cwd="/", env=environment, check=False,
        )
        return result.returncode


def _rollback_busy_operation(run_command) -> str:
    """Check only operations that do not coordinate through our maintenance lock."""
    for unit in ("uupd.service", "uupd-manual.service", "bazzite-portal-rollback.service"):
        text = run_command(["systemctl", "show", unit, "--property=LoadState,ActiveState"], timeout=15)
        properties = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        load = properties.get("LoadState")
        active = properties.get("ActiveState")
        if load == "not-found":
            continue
        if load != "loaded" or not active:
            raise RuntimeError(f"Could not verify system operation state for {unit}.")
        if active in runtime.ACTIVE_STATES:
            return unit
    return ""


def dispatch(operation: str, payload: str) -> int:
    """Call the existing operation entry point with its already-validated payload."""
    if operation == "rollback":
        return _rollback()
    module_spec = _MODULES.get(operation)
    if module_spec is None:
        raise ValueError("Unsupported maintenance operation.")
    module_name, argument_shape = module_spec
    if argument_shape != "one":
        raise ValueError("Unsupported maintenance argument shape.")
    module = importlib.import_module(f".{module_name}", __package__)
    return int(module.main([payload]))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("Usage: python3 -m bazzite_portal.maintenance.service <operation-payload>", file=sys.stderr)
        return 2
    parsed = parse_instance(argv[0])
    if parsed is None:
        print("Invalid maintenance service instance.", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1

    os.environ["LC_ALL"] = "C"
    try:
        os.chdir("/")
        return dispatch(*parsed)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(str(error)[:1024], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
