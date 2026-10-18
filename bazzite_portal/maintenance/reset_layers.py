"""Bounded, identity-checked removal of requested rpm-ostree layers."""
from __future__ import annotations
from bazzite_portal.core.i18n import C_, N_

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable

from bazzite_portal.maintenance import cancel_pending

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+.:~-]+(?:\.[A-Za-z0-9_+.:~-]+)?(?:-[A-Za-z0-9_.+~^%:-]+)?$")
PACKAGE_FIELDS = ("requested-packages", "requested-local-packages", "requested-local-fileoverride-packages")
MAX_PACKAGES = 256
MAX_PACKAGE_LENGTH = 256
MAX_IDENTITY_BYTES = 128 * 1024
LAYER_UNIT_PATTERNS = (
    re.compile(r"^bazzite-portal-maintenance@reset-layers-[0-9a-f]{64}\.service$"),
    re.compile(r"^bazzite-portal-maintenance@remove-selected-layers-[0-9a-f]{64}-[0-9a-f]{64}\.service$"),
)


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "", "packages": [], "identity": "",
            "stateroot": "", "deployment_id": "", "applied_packages": [],
            "inactive_packages": [], "inactive_known": False}


def target(data: Any) -> dict[str, Any]:
    """Return the complete removable request set for the sole booted RPM deployment."""
    if not isinstance(data, dict) or "deployments" not in data:
        return _unavailable("RPM-OSTree deployment data is unavailable.")
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        return _unavailable("RPM-OSTree deployment list is missing or empty.")
    if any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool) for item in deployments):
        return _unavailable("RPM-OSTree deployment list is malformed.")
    if sum(item["booted"] for item in deployments) != 1:
        return _unavailable("Expected exactly one booted deployment.")
    if deployments[0].get("booted") is not True:
        return _unavailable(N_("A different deployment is selected for next boot."))
    if data.get("transaction") not in (None, False, "", [], {}):
        return _unavailable(C_('An {rpm_ostree} transaction is active.', rpm_ostree='RPM-OSTree'))
    default = deployments[0]
    booted = next(item for item in deployments if item["booted"])
    checksum = booted.get("checksum")
    if not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum):
        return _unavailable("The booted deployment checksum is missing or malformed.")
    stateroot = booted.get("osname")
    deployment_id = booted.get("id")
    if not isinstance(stateroot, str) or not stateroot.strip() or not isinstance(deployment_id, str) or not deployment_id.strip():
        return _unavailable("The booted deployment stateroot identity is missing or malformed.")
    if default.get("osname") != stateroot or not isinstance(default.get("id"), str) or not default.get("id").strip():
        return _unavailable("The default deployment does not have a matching stateroot identity.")
    requested: list[str] = []
    for field in PACKAGE_FIELDS:
        values = default.get(field)
        if not isinstance(values, list):
            return _unavailable("The booted deployment has malformed {field} data.".format(field=field))
        for value in values:
            if (not isinstance(value, str) or not value or len(value) > MAX_PACKAGE_LENGTH
                    or not PACKAGE_PATTERN.fullmatch(value) or any(ord(c) < 32 for c in value)):
                return _unavailable("The booted deployment has a malformed requested package name.")
            requested.append(value)
    packages = sorted(set(requested))
    if len(packages) > MAX_PACKAGES:
        return _unavailable("The requested package list is too large to confirm safely.")
    applied_value = default.get("packages")
    applied_known = isinstance(applied_value, list) and len(applied_value) <= MAX_PACKAGES
    if applied_known:
        applied_known = all(isinstance(value, str) and value and len(value) <= MAX_PACKAGE_LENGTH
                            and PACKAGE_PATTERN.fullmatch(value) and not any(ord(c) < 32 for c in value)
                            for value in applied_value)
    applied = sorted(set(applied_value)) if applied_known else None
    inactive = sorted(set(booted["requested-packages"]) - set(applied)) if applied_known else []
    identity_data = {"checksum": checksum.lower(), "id": deployment_id, "osname": stateroot,
                     "requests": packages, "applied": applied}
    canonical = json.dumps(identity_data, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(canonical) > MAX_IDENTITY_BYTES:
        return _unavailable("The requested package list is too large to confirm safely.")
    identity = checksum.lower()
    token = hashlib.sha256(canonical).hexdigest()
    return {"available": bool(packages), "reason": "" if packages else N_("No requested package layers are present."),
            "token": token if packages else "", "packages": packages, "identity": identity,
            "stateroot": stateroot, "deployment_id": deployment_id,
            "applied_packages": applied or [], "inactive_packages": inactive,
            "inactive_known": applied_known}


def _command(argv, timeout=15):
    return cancel_pending._command(argv, timeout)


def _read_status(run_command):
    try:
        data = json.loads(run_command(["rpm-ostree", "status", "--json"], timeout=15))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def _busy(run_command):
    for unit in cancel_pending.OPERATION_UNITS:
        text = run_command(["systemctl", "show", unit, "--property=LoadState,ActiveState"], timeout=15)
        props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            continue
        if props.get("LoadState") != "loaded" or not props.get("ActiveState"):
            raise RuntimeError(f"Could not verify system operation state for {unit}.")
        if props["ActiveState"] in cancel_pending.ACTIVE_STATES:
            return unit
    text = run_command(["systemctl", "list-units", "--all", "--no-pager", "--output=json",
                        "bazzite-portal-*@*.service"], timeout=15)
    try:
        records = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("Could not verify package-layer removal state.") from exc
    if not isinstance(records, list):
        raise RuntimeError("Could not verify package-layer removal state.")
    units = list(dict.fromkeys(row.get("unit") for row in records if isinstance(row, dict)
                               and isinstance(row.get("unit"), str)
                               and any(pattern.fullmatch(row["unit"]) for pattern in LAYER_UNIT_PATTERNS)))
    if len(units) > 32:
        raise RuntimeError("Too many package-layer removal units to inspect safely.")
    own_invocation = next((value for value in (os.environ.get("INVOCATION_ID", ""),
                                                os.environ.get("SYSTEMD_INVOCATION_ID", ""))
                           if re.fullmatch(r"[0-9a-fA-F]{32}", value)), "")
    for unit in units:
        text = run_command(["systemctl", "show", unit,
                            "--property=LoadState,ActiveState,InvocationID"], timeout=15)
        props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            continue
        if props.get("LoadState") != "loaded" or not props.get("ActiveState"):
            raise RuntimeError(f"Could not verify package-layer removal state for {unit}.")
        if props["ActiveState"] in cancel_pending.ACTIVE_STATES:
            if own_invocation and props.get("InvocationID", "").lower() == own_invocation.lower():
                continue
            return unit
    text = run_command(["systemctl", "list-units", "--all", "--no-pager", "--output=json", "bazzite-portal-maintenance@cancel-pending-*.service"], timeout=15)
    try:
        records = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("Could not verify pending-deployment cancellation state.") from exc
    if not isinstance(records, list):
        raise RuntimeError("Could not verify pending-deployment cancellation state.")
    units = list(dict.fromkeys(row.get("unit") for row in records if isinstance(row, dict)
                               and isinstance(row.get("unit"), str)
                               and re.fullmatch(r"bazzite-portal-maintenance@cancel-pending-[0-9a-f]{64}\.service", row["unit"])))
    if len(units) > 32:
        raise RuntimeError("Too many pending-deployment cancellation units to inspect safely.")
    for unit in units:
        text = run_command(["systemctl", "show", unit, "--property=LoadState,ActiveState"], timeout=15)
        props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            continue
        if props.get("LoadState") != "loaded" or not props.get("ActiveState"):
            raise RuntimeError(f"Could not verify system operation state for {unit}.")
        if props["ActiveState"] in cancel_pending.ACTIVE_STATES:
            return unit
    return ""


def execute(token: str, command_runner: Callable[..., str] = _command,
            lock_factory: Callable[[], Any] = cancel_pending._pending_lock,
            selected_packages: list[str] | tuple[str, ...] | None = None) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid package-layer removal token.")
    with lock_factory():
        for _ in range(2):
            current = target(_read_status(command_runner))
            if not current["available"] or current["token"] != token:
                raise RuntimeError(current["reason"] or "The requested package list changed after confirmation.")
            chosen = current["packages"] if selected_packages is None else selected_packages
            if (not isinstance(chosen, (list, tuple)) or not chosen
                    or any(not isinstance(name, str) or name not in current["packages"] for name in chosen)
                    or len(set(chosen)) != len(chosen)):
                raise ValueError("Selected packages must be a nonempty unique subset of the confirmed request list.")
            busy = _busy(command_runner)
            if busy:
                raise RuntimeError(f"Cannot remove package layers while {busy} is running.")
        # The legacy API retains its all-overlays behavior. The selectable path
        # passes validated package names as argv elements, never shell text.
        if selected_packages is None:
            command_runner(["rpm-ostree", "reset", "--overlays"], timeout=None)
        else:
            command_runner(["rpm-ostree", "uninstall", *chosen], timeout=None)
        after_data = _read_status(command_runner)
        deployments = after_data.get("deployments")
        if not isinstance(deployments, list) or any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool) for item in deployments):
            raise RuntimeError("Could not verify the booted deployment after package-layer removal.")
        booted = [item for item in deployments if item["booted"]]
        if len(booted) != 1 or not isinstance(booted[0].get("checksum"), str):
            raise RuntimeError("Could not verify the booted deployment after package-layer removal.")
        if booted[0]["checksum"].lower() != current["identity"]:
            raise RuntimeError("Package-layer removal changed the booted deployment identity.")
        if (not deployments or deployments[0].get("osname") != current["stateroot"]
                or booted[0].get("osname") != current["stateroot"]):
            raise RuntimeError("The default deployment no longer matches the confirmed booted stateroot.")
        if not isinstance(deployments[0].get("id"), str) or not deployments[0]["id"].strip():
            raise RuntimeError("Could not verify the default deployment identity after removal.")
        remaining = []
        for field in PACKAGE_FIELDS:
            values = deployments[0].get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise RuntimeError("Could not verify the requested package list after removal.")
            remaining.extend(values)
        expected = set(current["packages"]) - set(chosen)
        if set(remaining) != expected or len(remaining) != len(expected):
            raise RuntimeError("The remaining requested package layers do not match the confirmed selection.")
    return "Requested package layers removed."


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.reset_layers <64-character-token>", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1
    try:
        print(execute(argv[0]))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
