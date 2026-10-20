"""Identity-checked removal of one staged rpm-ostree deployment."""

from __future__ import annotations
from bazzite_portal.core.i18n import C_, N_

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable
from bazzite_portal.maintenance.status import read_status as _read_status
from bazzite_portal.maintenance import runtime


TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = runtime.CHECKSUM_PATTERN


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "", "version": "Unknown", "image": "Unknown", "identity": "Unknown"}


def _token(identity: tuple[str, str, str, str]) -> str:
    deployment_id, checksum, osname, container_reference = identity
    canonical = json.dumps(
        {"id": deployment_id, "checksum": checksum, "osname": osname,
         "container-image-reference": container_reference},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def target(data: Any) -> dict[str, Any]:
    """Describe the only rpm-ostree target this workflow may cancel."""
    if not isinstance(data, dict):
        return _unavailable("Deployment status is not an object.")
    if "deployments" not in data:
        if "status" in data:
            return _unavailable(C_("{bootc} deployments cannot be canceled by this workflow.", bootc="Bootc"))
        return _unavailable("RPM-OSTree deployment data is unavailable.")
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        return _unavailable("RPM-OSTree deployment list is missing or empty.")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("booted"), bool) for entry in deployments):
        return _unavailable("RPM-OSTree deployment list is malformed.")
    if sum(entry["booted"] for entry in deployments) != 1:
        return _unavailable("Expected exactly one running deployment.")
    if runtime.has_transaction(data.get("transaction")):
        return _unavailable(C_('An {rpm_ostree} transaction is active.', rpm_ostree='RPM-OSTree'))

    pending = deployments[0]
    booted = next(entry for entry in deployments if entry["booted"] is True)
    pending_osname = pending.get("osname")
    booted_osname = booted.get("osname")
    if not isinstance(pending_osname, str) or not pending_osname.strip():
        return _unavailable("The pending deployment has no stateroot identity.")
    if not isinstance(booted_osname, str) or not booted_osname.strip():
        return _unavailable("The running deployment has no stateroot identity.")
    if pending_osname != booted_osname:
        return _unavailable("The pending deployment belongs to a different stateroot.")
    if pending["booted"] is not False:
        return _unavailable(N_("There is no pending deployment."))
    if pending.get("staged") is not True:
        return _unavailable(N_("The next boot is a scheduled rollback or other non-staged deployment."))
    if pending.get("pinned") is not False:
        return _unavailable(N_("The pending deployment is pinned or its pin state is unknown."))
    identity = runtime.canonical_deployment_identity(pending, pending_osname)
    if identity is None:
        return _unavailable("The pending deployment identity is incomplete or malformed.")
    deployment_id, checksum, _osname, container_reference = identity
    return {
        "available": True,
        "reason": "",
        "token": _token(identity),
        "version": runtime.clean_text(pending.get("version")),
        "image": runtime.clean_text(container_reference),
        "identity": runtime.clean_text(checksum),
    }


def _remaining_deployments(data: dict[str, Any], canceled_id: str, canceled_checksum: str = "") -> list[tuple[str, str, bool, bool]]:
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        raise RuntimeError("RPM-OSTree deployment list is missing or empty.")
    remaining = []
    for entry in deployments:
        if not isinstance(entry, dict) or not isinstance(entry.get("booted"), bool):
            raise RuntimeError("RPM-OSTree deployment list is malformed.")
        deployment_id = entry.get("id")
        checksum = entry.get("checksum")
        if not isinstance(deployment_id, str) or not deployment_id or not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum):
            raise RuntimeError("A retained deployment has no stable identity.")
        if deployment_id == canceled_id and checksum.lower() == canceled_checksum.lower():
            continue
        remaining.append((deployment_id, checksum.lower(), entry["booted"], entry.get("pinned") is True))
    if sum(entry[2] for entry in remaining) != 1:
        raise RuntimeError("Expected exactly one retained running deployment.")
    return remaining


def execute(
    token: str,
    command_runner: Callable[..., str] = runtime.command,
    lock_factory: Callable[[], Any] = runtime.maintenance_lock,
) -> str:
    """Revalidate a confirmed token, remove only its pending deployment, verify."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid cancellation token.")
    with lock_factory():
        initial = _read_status(command_runner)
        initial_target = target(initial)
        if not initial_target["available"] or initial_target["token"] != token:
            raise RuntimeError(initial_target["reason"] or "The pending deployment changed after confirmation.")
        active = runtime.active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot cancel a pending deployment while {active} is running.")

        # Re-read immediately before cleanup so a changed deployment or newly
        # started managed job cannot pass using the first snapshot.
        current = _read_status(command_runner)
        current_target = target(current)
        if not current_target["available"] or current_target["token"] != token:
            raise RuntimeError(current_target["reason"] or "The pending deployment changed before cleanup.")
        active = runtime.active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot cancel a pending deployment while {active} is running.")

        canceled_checksum = current_target["identity"]
        canceled_id = current["deployments"][0]["id"]
        retained_before = _remaining_deployments(current, canceled_id, canceled_checksum)
        if command_runner is runtime.command:
            print("Removing the confirmed pending deployment...", flush=True)
        # cleanup --pending removes ALL unpinned deployments before the booted
        # one, including a saved deployment selected by an earlier rollback.
        # The confirmed staged target is index zero; remove only that entry.
        command_runner(["ostree", "admin", "undeploy", "0"], timeout=None)
        after = _read_status(command_runner)
        after_deployments = after.get("deployments")
        if not isinstance(after_deployments, list):
            raise RuntimeError("Could not verify deployments after cleanup.")
        if any(
            isinstance(entry, dict) and entry.get("id") == canceled_id
            and isinstance(entry.get("checksum"), str) and entry["checksum"].lower() == canceled_checksum.lower()
            for entry in after_deployments
        ):
            raise RuntimeError("The confirmed pending deployment is still present after cleanup.")
        retained_after = _remaining_deployments(after, canceled_id, canceled_checksum)
        if retained_after != retained_before:
            raise RuntimeError("Cleanup changed the running or saved deployments unexpectedly.")
    return "Pending deployment removed."


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.cancel_pending <64-character-token>", file=sys.stderr)
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
